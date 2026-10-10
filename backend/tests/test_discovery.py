"""
tests/test_discovery.py
────────────────────────
Tests for services/requisition/discovery.py and contacts.py.
No real network calls – all HTTP is mocked via unittest.mock.
No LLM imports.
"""
from __future__ import annotations

import io
import json
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, PropertyMock


# ─── helpers to get modules without importing the whole app ──────────────────

def _import_discovery():
    import importlib
    import services.requisition.discovery as m
    importlib.reload(m)
    return m


def _import_contacts():
    import importlib
    import services.requisition.contacts as m
    importlib.reload(m)
    return m


# ─── Shared fake bank HTML fixture ───────────────────────────────────────────

_FAKE_HTML = b"""
<html><body>
<h1>State Bank of India - Nodal Officer Contact</h1>
<p>For law enforcement and LEA requests please write to
nodal.lea@sbi.co.in or call our cyber cell.</p>
<p>General queries: customercare@sbi.co.in</p>
<a href="mailto:nodal.lea@sbi.co.in">Email Nodal Officer</a>
<p>Also reach us at sbi [at] support [dot] co [dot] in</p>
</body></html>
"""

_FAKE_HTML_OTHER_BANK = b"""
<html><body>
<h1>Bank of Baroda - Customer Support</h1>
<p>Write to us at info@bankofbaroda.in</p>
</body></html>
"""

_SMALL_BANK_HTML = b"""
<html><body>
<h1>HDFC Bank Nodal Officer</h1>
<p>Please contact nodal@hdfcbank.com for police and cyber requests.</p>
</body></html>
"""


# ─── 1. HTML text extraction ─────────────────────────────────────────────────

class TestHTMLExtraction(unittest.TestCase):

    def setUp(self):
        self.mod = _import_discovery()

    def test_extracts_plain_email(self):
        text, mailtos = self.mod._extract_text_from_html(_FAKE_HTML)
        self.assertIn("nodal.lea@sbi.co.in", text)

    def test_extracts_mailto_link(self):
        _, mailtos = self.mod._extract_text_from_html(_FAKE_HTML)
        self.assertIn("nodal.lea@sbi.co.in", mailtos)

    def test_strips_script_tags(self):
        html = b"<html><script>var x = 'secret@x.com';</script><body>ok@good.com</body></html>"
        text, _ = self.mod._extract_text_from_html(html)
        self.assertNotIn("secret@x.com", text)
        self.assertIn("ok@good.com", text)

    def test_strips_style_tags(self):
        html = b"<html><style>.a { color: red; }</style><body>visible@bank.in</body></html>"
        text, _ = self.mod._extract_text_from_html(html)
        self.assertNotIn("color", text)

    def test_decodes_obfuscation_at(self):
        text = "contact us at sbi [at] nodal [dot] co [dot] in for help"
        decoded = self.mod._decode_obfuscations(text)
        self.assertIn("sbi@nodal.co.in", decoded)

    def test_decodes_at_word(self):
        text = "email us: fraud at sbibank.in today"
        decoded = self.mod._decode_obfuscations(text)
        self.assertIn("fraud@sbibank.in", decoded)


# ─── 2. Email validation ─────────────────────────────────────────────────────

class TestEmailValidation(unittest.TestCase):

    def setUp(self):
        self.mod = _import_discovery()

    def test_rejects_noreply(self):
        self.assertFalse(self.mod._is_valid_candidate("noreply@bank.in"))

    def test_rejects_example_domain(self):
        self.assertFalse(self.mod._is_valid_candidate("officer@example.com"))

    def test_rejects_too_long(self):
        long_email = "a" * 90 + "@bank.in"
        self.assertFalse(self.mod._is_valid_candidate(long_email))

    def test_accepts_valid_nodal(self):
        self.assertTrue(self.mod._is_valid_candidate("nodal.lea@sbi.co.in"))

    def test_accepts_generic_but_valid(self):
        # Generic emails are valid candidates, just scored lower
        self.assertTrue(self.mod._is_valid_candidate("customercare@sbi.co.in"))


# ─── 3. Scoring ──────────────────────────────────────────────────────────────

class TestScoring(unittest.TestCase):

    def setUp(self):
        self.mod = _import_discovery()

    def _score(self, email, text, bank, url, rank=1):
        return self.mod._score_email(email, text, bank, url, rank)

    def test_nodal_email_scores_higher_than_generic(self):
        bank = "State Bank of India"
        text = (
            "For nodal officer law enforcement requests: nodal.lea@sbi.co.in\n"
            "Customer care: customercare@sbi.co.in"
        )
        nodal_score, _, _  = self._score("nodal.lea@sbi.co.in",   text, bank, "https://sbi.co.in")
        generic_score, generic_flag, _ = self._score("customercare@sbi.co.in", text, bank, "https://sbi.co.in")

        self.assertGreater(nodal_score, generic_score)
        self.assertTrue(generic_flag, "customercare should be flagged generic")

    def test_domain_match_adds_score(self):
        bank = "HDFC Bank"
        text = "fraud@hdfcbank.com"
        score, _, _ = self._score("fraud@hdfcbank.com", text, bank, "https://hdfcbank.com")
        self.assertGreaterEqual(score, self.mod.S_DOMAIN_MATCH)

    def test_context_keyword_adds_score(self):
        bank = "Axis Bank"
        text = "For police and cyber investigations write to cyber@axisbank.com"
        score, _, _ = self._score("cyber@axisbank.com", text, bank, "https://axisbank.com")
        self.assertGreaterEqual(score, self.mod.S_CONTEXT_MATCH)

    def test_different_bank_page_gives_low_score(self):
        """A page about a different bank should be skipped by bank_tokens_in_text check."""
        bank = "State Bank of India"
        # Only mentions Bank of Baroda
        text_baroda = "Bank of Baroda customer service: care@bankofbaroda.in"
        self.assertFalse(self.mod._bank_tokens_in_text(bank, text_baroda))

    def test_generic_penalty_applied(self):
        bank = "Canara Bank"
        text = "contact helpdesk@canarabank.in for support"
        score, is_generic, _ = self._score("helpdesk@canarabank.in", text, bank, "https://canarabank.in")
        self.assertTrue(is_generic)
        self.assertLess(score, 0.5)

    def test_score_clamped_0_to_1(self):
        bank = "SBI"
        text = "nodal.lea@sbi.co.in nodal law enforcement police cyber investigat"
        score, _, _ = self._score("nodal.lea@sbi.co.in", text, bank, "https://sbi.co.in", rank=1)
        self.assertLessEqual(score, 1.0)
        self.assertGreaterEqual(score, 0.0)


# ─── 4. SSRF guard ───────────────────────────────────────────────────────────

class TestSSRFGuard(unittest.TestCase):

    def setUp(self):
        self.mod = _import_discovery()

    def test_rejects_loopback(self):
        self.assertFalse(self.mod._is_safe_url("http://127.0.0.1/admin"))

    def test_rejects_link_local(self):
        self.assertFalse(self.mod._is_safe_url("http://169.254.169.254/latest/meta-data/"))

    def test_rejects_file_scheme(self):
        self.assertFalse(self.mod._is_safe_url("file:///etc/passwd"))

    def test_rejects_non_standard_port(self):
        self.assertFalse(self.mod._is_safe_url("https://sbi.co.in:8443/api"))

    def test_accepts_standard_https(self):
        # Mock the socket lookup inside the discovery module's namespace.
        # 104.21.80.100 is a real Cloudflare IP (public, not reserved).
        import services.requisition.discovery as disc
        with patch.object(disc.socket, "getaddrinfo",
                          return_value=[(None, None, None, None, ("104.21.80.100", None))]):
            result = disc._is_safe_url("https://test-bank-not-real.example/contact")
        self.assertTrue(result)


# ─── 5. Search provider: none ────────────────────────────────────────────────

class TestSearchProviderNone(unittest.TestCase):

    def setUp(self):
        self.mod = _import_discovery()
        self.mod._WARNED_NO_PROVIDER = False  # reset warning flag

    def test_none_provider_returns_empty(self):
        with patch.dict("os.environ", {"SEARCH_PROVIDER": "none", "SEARCH_API_KEY": ""}):
            result = self.mod._search("test query")
        self.assertEqual(result, [])

    def test_no_api_key_returns_empty(self):
        with patch.dict("os.environ", {"SEARCH_PROVIDER": "serper", "SEARCH_API_KEY": ""}):
            result = self.mod._search("test query")
        self.assertEqual(result, [])


# ─── 6. Timeout for one bank doesn't block others ────────────────────────────

class TestDiscoveryTimeout(unittest.TestCase):

    def test_timeout_bank_gets_not_found(self):
        import threading
        contacts_mod = _import_contacts()

        stop_event = threading.Event()

        def _blocking_discover(bank_name):
            if bank_name == "Slow Bank":
                # Block until test finishes (simulates network timeout)
                stop_event.wait(timeout=10)
            return []

        with patch.object(contacts_mod, "discover_bank_contact", side_effect=_blocking_discover), \
             patch.object(contacts_mod, "_get_active_contacts", return_value=[]):
            # Use a 1-second discovery timeout
            original = contacts_mod._DISCOVERY_TIMEOUT
            contacts_mod._DISCOVERY_TIMEOUT = 1
            try:
                results = contacts_mod.resolve_contacts(
                    [("slow_bank", "Slow Bank"), ("hdfc_bank", "HDFC Bank")],
                    run_discovery=True,
                )
            finally:
                contacts_mod._DISCOVERY_TIMEOUT = original
                stop_event.set()  # unblock the thread so it can terminate

        # Both should have results (not hung)
        self.assertIn("slow_bank", results)
        self.assertIn("hdfc_bank", results)
        self.assertEqual(results["slow_bank"].status, "not_found")


# ─── 7. Resolve: verified directory entry wins ─────────────────────────────

class TestResolveContacts(unittest.TestCase):

    def setUp(self):
        self.mod = _import_contacts()

    def _fake_row(self, email, verified=1, confidence=0.85, source="directory"):
        return {
            "id": 1, "email": email, "cc_emails": None, "source": source,
            "source_url": "https://example-bank.in/contact",
            "confidence": confidence, "evidence": "LEA nodal officer",
            "verified": verified, "last_checked_at": None,
        }

    def test_verified_wins_without_http_call(self):
        with patch.object(self.mod, "_get_active_contacts",
                          return_value=[self._fake_row("nodal@sbi.co.in", verified=1)]), \
             patch.object(self.mod, "discover_bank_contact") as mock_disc:
            result = self.mod.resolve_contacts([("sbi", "State Bank of India")], run_discovery=True)
        mock_disc.assert_not_called()
        self.assertEqual(result["sbi"].status, "verified")
        self.assertEqual(result["sbi"].to, ["nodal@sbi.co.in"])

    def test_upi_merchant_skipped(self):
        with patch.object(self.mod, "discover_bank_contact") as mock_disc:
            result = self.mod.resolve_contacts(
                [("upi_merchant", "paytm@oksbi")], run_discovery=True
            )
        mock_disc.assert_not_called()
        self.assertEqual(result["upi_merchant"].status, "not_found")

    def test_unknown_bank_key_skipped(self):
        result = self.mod.resolve_contacts([("__unknown__", "")], run_discovery=True)
        self.assertEqual(result["__unknown__"].status, "not_found")

    def test_bank_of_india_ne_bank_of_baroda(self):
        """Token-set normalizer must not confuse Bank of India with Bank of Baroda."""
        from backend.services.email.bank_names import normalize_bank_name
        norm_boi = normalize_bank_name("Bank of India")
        norm_bob = normalize_bank_name("Bank of Baroda")
        self.assertNotEqual(norm_boi, norm_bob)

    def test_bank_of_india_ne_indian_bank(self):
        from backend.services.email.bank_names import normalize_bank_name
        norm_boi = normalize_bank_name("Bank of India")
        norm_ib  = normalize_bank_name("Indian Bank")
        self.assertNotEqual(norm_boi, norm_ib)


# ─── 8. PDF extraction ───────────────────────────────────────────────────────

class TestPDFExtraction(unittest.TestCase):

    def setUp(self):
        self.mod = _import_discovery()

    def test_pdf_extraction_returns_text(self):
        """Create a tiny real PDF with pdfplumber-compatible content using pdfplumber's test fixtures."""
        try:
            import pdfplumber
        except ImportError:
            self.skipTest("pdfplumber not installed")

        # Write a minimal one-page PDF using reportlab if available, else skip
        try:
            from reportlab.pdfgen import canvas  # type: ignore
            buf = io.BytesIO()
            c = canvas.Canvas(buf)
            c.drawString(72, 720, "Please contact lea@testbank.in for LEA requests.")
            c.save()
            pdf_bytes = buf.getvalue()
        except ImportError:
            self.skipTest("reportlab not installed; skipping PDF fixture test")

        text = self.mod._extract_text_from_pdf(pdf_bytes)
        self.assertIn("lea@testbank.in", text)


# ─── 9. No LLM import ────────────────────────────────────────────────────────

class TestNoLLMInDiscovery(unittest.TestCase):

    def _all_py_files(self):
        import pathlib
        pkg = pathlib.Path(__file__).parent.parent / "services" / "requisition"
        return list(pkg.rglob("*.py"))

    def test_no_openrouter_import(self):
        for path in self._all_py_files():
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "request_openrouter_completion", content,
                msg=f"{path} references request_openrouter_completion",
            )

    def test_no_raw_requests_import_in_contacts(self):
        """contacts.py must not directly import requests (discovery.py does, that's OK)."""
        import pathlib
        path = pathlib.Path(__file__).parent.parent / "services" / "requisition" / "contacts.py"
        content = path.read_text(encoding="utf-8")
        pattern = re.compile(r"^\s*(import requests|from requests\b)", re.MULTILINE)
        self.assertIsNone(
            pattern.search(content),
            msg="contacts.py should not import requests directly",
        )


if __name__ == "__main__":
    unittest.main()
