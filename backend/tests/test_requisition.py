"""
tests/test_requisition.py
──────────────────────────
Unit tests for the services/requisition package.
No network, no real SMTP, no DB connections.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import types
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

from backend.services.email.bank_names import normalize_bank_name
from backend.services.email.collect import collect_requisition_items, ACTIONABLE_CODES
from services.email.template import build_email
import services.email.mailer as mailer_module


# ─────────────────────────────────────────────────────────────────────────────
# 1. normalize_bank_name
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeBankName:
    def test_word_order_invariant(self):
        assert normalize_bank_name("Kotak Mahindra Bank") == normalize_bank_name("Bank Kotak Mahindra")

    def test_limited_synonym(self):
        assert normalize_bank_name("Ratnakar Bank Limited") == normalize_bank_name("Ratnakar Bank Ltd")

    def test_bank_of_india_ne_bank_of_baroda(self):
        assert normalize_bank_name("Bank of India") != normalize_bank_name("Bank of Baroda")

    def test_bank_of_india_ne_indian_bank(self):
        assert normalize_bank_name("Bank of India") != normalize_bank_name("Indian Bank")

    def test_private_synonym(self):
        assert normalize_bank_name("HDFC Bank Private Limited") == normalize_bank_name("HDFC Bank Pvt Ltd")

    def test_empty_returns_unknown(self):
        assert normalize_bank_name("") == "__unknown__"
        assert normalize_bank_name("   ") == "__unknown__"

    def test_punctuation_removed(self):
        # "Kotak Mahindra Bank." and "Kotak Mahindra Bank" should normalize the same
        assert normalize_bank_name("Kotak Mahindra Bank.") == normalize_bank_name("Kotak Mahindra Bank")

    def test_case_insensitive(self):
        assert normalize_bank_name("AXIS BANK") == normalize_bank_name("axis bank")

    def test_deduplication(self):
        # duplicate tokens only appear once
        result = normalize_bank_name("Bank Bank Bank")
        assert result.count("bank") == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. collect_requisition_items
# ─────────────────────────────────────────────────────────────────────────────

def _make_node_item(bank="Test Bank", codes=None, node_id="node:abc", **extra):
    codes = codes or ["END_NO_STATUS"]
    return {
        "kind": "node",
        "nodeId": node_id,
        "bank": bank,
        "accountNo": extra.get("accountNo", "1234567890"),
        "utr": extra.get("utr", "UTR123"),
        "layer": extra.get("layer", 1),
        "txAmount": extra.get("txAmount", 5000),
        "disputedAmount": extra.get("disputedAmount", 5000),
        "unaccountedAmount": extra.get("unaccountedAmount", None),
        "reasons": [{"code": c, "message": c} for c in codes],
    }


def _make_hold_item(action_taken_by="PNB", hold_id="hold:xyz", **extra):
    return {
        "kind": "hold",
        "holdId": hold_id,
        "accountNo": extra.get("accountNo", "9876543210"),
        "amount": extra.get("amount", 2000),
        "date": extra.get("date", "2024-01-01"),
        "actionTakenBy": action_taken_by,
        "reasons": [{"code": "HOLD_WITHOUT_TRAIL", "message": "Hold without trail"}],
    }


def _make_payload(incomplete=None, orphan_holds=None):
    return {
        "incomplete": incomplete or [],
        "orphanHolds": orphan_holds or [],
    }


class TestCollectRequisitionItems:
    def test_groups_by_bank(self):
        payload = _make_payload(
            incomplete=[
                _make_node_item("PNB", node_id="n1"),
                _make_node_item("PNB", node_id="n2", accountNo="9999"),
                _make_node_item("SBI", node_id="n3"),
            ]
        )
        buckets, skipped = collect_requisition_items(payload)
        pnb_key = normalize_bank_name("PNB")
        sbi_key = normalize_bank_name("SBI")
        assert pnb_key in buckets
        assert sbi_key in buckets
        assert len(buckets[pnb_key]["items"]) == 2
        assert len(buckets[sbi_key]["items"]) == 1
        assert skipped == []

    def test_internal_only_codes_go_to_skipped(self):
        payload = _make_payload(
            incomplete=[
                _make_node_item("SBI", codes=["VICTIM_UNTRACED"], node_id="n1"),
                _make_node_item("SBI", codes=["AMOUNT_ESTIMATED"], node_id="n2"),
                _make_node_item("SBI", codes=["LOW_CONFIDENCE_LINK"], node_id="n3"),
                _make_node_item("SBI", codes=["END_NO_STATUS"], node_id="n4"),  # actionable
            ]
        )
        buckets, skipped = collect_requisition_items(payload)
        assert len(skipped) == 3
        skipped_ids = {s["refId"] for s in skipped}
        assert "n1" in skipped_ids
        assert "n2" in skipped_ids
        assert "n3" in skipped_ids
        sbi_key = normalize_bank_name("SBI")
        assert sbi_key in buckets
        assert len(buckets[sbi_key]["items"]) == 1

    def test_mixed_codes_include_if_any_actionable(self):
        payload = _make_payload(
            incomplete=[
                _make_node_item("HDFC", codes=["END_NO_STATUS", "VICTIM_UNTRACED"], node_id="n1"),
            ]
        )
        buckets, skipped = collect_requisition_items(payload)
        assert len(skipped) == 0
        hdfc_key = normalize_bank_name("HDFC")
        assert hdfc_key in buckets
        item = buckets[hdfc_key]["items"][0]
        # Only actionable codes in the asks
        assert "END_NO_STATUS" in item["reasonCodes"]
        assert "VICTIM_UNTRACED" not in item["reasonCodes"]

    def test_orphan_hold_goes_to_action_taken_by(self):
        payload = _make_payload(
            orphan_holds=[_make_hold_item("Punjab National Bank", hold_id="h1")]
        )
        buckets, skipped = collect_requisition_items(payload)
        pnb_key = normalize_bank_name("Punjab National Bank")
        assert pnb_key in buckets
        assert buckets[pnb_key]["items"][0]["refId"] == "h1"

    def test_missing_bank_becomes_unknown(self):
        payload = _make_payload(
            incomplete=[_make_node_item("", codes=["END_NO_STATUS"], node_id="n1")]
        )
        buckets, skipped = collect_requisition_items(payload)
        assert "__unknown__" in buckets
        assert buckets["__unknown__"]["bankName"] == "Bank not identified"

    def test_item_hash_stable_on_order_shuffle(self):
        items = [
            _make_node_item("SBI", node_id=f"n{i}", accountNo=f"ACC{i}")
            for i in range(5)
        ]
        payload_a = _make_payload(incomplete=items)
        payload_b = _make_payload(incomplete=list(reversed(items)))
        buckets_a, _ = collect_requisition_items(payload_a)
        buckets_b, _ = collect_requisition_items(payload_b)
        key = normalize_bank_name("SBI")
        assert buckets_a[key]["itemHash"] == buckets_b[key]["itemHash"]

    def test_item_hash_changes_when_amount_changes(self):
        item_a = _make_node_item("SBI", node_id="n1", disputedAmount=5000)
        item_b = _make_node_item("SBI", node_id="n1", disputedAmount=9999)
        buckets_a, _ = collect_requisition_items(_make_payload(incomplete=[item_a]))
        buckets_b, _ = collect_requisition_items(_make_payload(incomplete=[item_b]))
        key = normalize_bank_name("SBI")
        assert buckets_a[key]["itemHash"] != buckets_b[key]["itemHash"]

    def test_empty_payload_returns_empty(self):
        buckets, skipped = collect_requisition_items({"incomplete": [], "orphanHolds": []})
        assert buckets == {}
        assert skipped == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. build_email
# ─────────────────────────────────────────────────────────────────────────────

def _officer():
    return {
        "inspectorName":   "Ravi Kumar",
        "inspectorRank":   "Inspector",
        "inspectorBranch": "Cyber Cell, Mumbai",
    }


def _item(account="ACC123", utr="UTR456", amount="5000", disputed="5000", asks=None):
    return {
        "accountNo":           account,
        "utr":                 utr,
        "transactionDateTime": "2024-01-15 10:30:00",
        "transactionAmount":   amount,
        "disputedAmount":      disputed,
        "unaccountedAmount":   "",
        "reasonCodes":         ["END_NO_STATUS"],
        "asks":                asks or [
            "Please state where the credited amount was transferred or withdrawn."
        ],
    }


class TestBuildEmail:
    def test_account_utr_amount_present(self):
        result = build_email("Test Bank", [_item()], "ACK123", _officer(), "reply@test.com")
        body = result["body_text"]
        assert "ACC123" in body
        assert "UTR456" in body
        assert "5,000.00" in body

    def test_missing_utr_prints_not_available(self):
        item = _item(utr="")
        result = build_email("Test Bank", [item], "ACK123", _officer(), "reply@test.com")
        assert "Not available" in result["body_text"]
        assert "None" not in result["body_text"]

    def test_newlines_in_bank_name_cannot_create_extra_header_lines(self):
        # A bank name with embedded newline must not break the subject into two lines
        result = build_email("Evil\nBank", [_item()], "ACK123", _officer(), "reply@test.com")
        subject_lines = result["subject"].splitlines()
        # Subject must be exactly one line (CR/LF replaced by space)
        assert len(subject_lines) == 1

    def test_no_legal_line_when_env_empty(self, monkeypatch):
        monkeypatch.delenv("REQUISITION_LEGAL_REFERENCE", raising=False)
        result = build_email("Test Bank", [_item()], "ACK123", _officer(), "reply@test.com",
                             legal_reference="")
        # Body should not have a blank legal-reference slot
        assert "None" not in result["body_text"]
        assert "—" not in result["body_text"]

    def test_legal_line_present_when_provided(self):
        result = build_email("Test Bank", [_item()], "ACK123", _officer(), "reply@test.com",
                             legal_reference="Ref: Section 91 CrPC")
        assert "Ref: Section 91 CrPC" in result["body_text"]

    def test_refuses_without_ack_no(self):
        with pytest.raises(ValueError, match="ack_no"):
            build_email("Test Bank", [_item()], "", _officer(), "reply@test.com")

    def test_deterministic_identical_input(self):
        args = ("Test Bank", [_item()], "ACK123", _officer(), "reply@test.com")
        assert build_email(*args) == build_email(*args)

    def test_subject_format(self):
        result = build_email("PNB", [_item()], "ACK999", _officer(), "reply@test.com")
        assert result["subject"] == "Request for transaction details - Ack No. ACK999 - PNB"

    def test_control_chars_stripped_from_remark(self):
        item = _item()
        item["asks"] = ["Please clarify\x00\x1fthis."]
        result = build_email("Test Bank", [item], "ACK123", _officer(), "reply@test.com")
        assert "\x00" not in result["body_text"]
        assert "\x1f" not in result["body_text"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Mailer
# ─────────────────────────────────────────────────────────────────────────────

SMTP_ENV = {
    "SMTP_HOST":         "smtp.example.com",
    "SMTP_PORT":         "587",
    "SMTP_USERNAME":     "user@example.com",
    "SMTP_PASSWORD":     "password",
    "SMTP_FROM_ADDRESS": "from@example.com",
}


class TestMailer:
    def _send(self, to, cc, subject, body, reply_to, extra_env=None, **kwargs):
        env = {**SMTP_ENV, **(extra_env or {})}
        with patch.dict(os.environ, env, clear=False):
            with patch("smtplib.SMTP") as mock_smtp_cls:
                ctx = MagicMock()
                mock_smtp_cls.return_value.__enter__ = lambda s: ctx
                mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
                ctx.send_message = MagicMock()
                result = mailer_module.send_requisition_email(to, cc, subject, body, reply_to)
        return result, ctx

    def test_header_injection_in_to_raises(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            with pytest.raises(ValueError, match="Header injection"):
                mailer_module.send_requisition_email(
                    ["evil\r\nBcc:spam@bad.com"], [], "Subj", "Body", "r@x.com"
                )

    def test_header_injection_in_subject_raises(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            with pytest.raises(ValueError, match="Header injection"):
                mailer_module.send_requisition_email(
                    ["ok@test.com"], [], "Evil\nSubject", "Body", "r@x.com"
                )

    def test_test_mode_redirects_to_test_recipient(self, monkeypatch):
        test_env = {**SMTP_ENV, "REQUISITION_TEST_RECIPIENT": "test@safe.com"}
        with patch.dict(os.environ, test_env, clear=False):
            with patch("smtplib.SMTP") as mock_smtp_cls:
                ctx = MagicMock()
                mock_smtp_cls.return_value.__enter__ = lambda s: ctx
                mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
                ctx.send_message = MagicMock()
                msg_id, test_mode, delivered_to = mailer_module.send_requisition_email(
                    ["bank@pnb.in"], ["cc@pnb.in"], "Subject", "Body", "r@x.com"
                )
        assert test_mode is True
        # Verify the message sent to SMTP has test recipient, not real bank
        call_args = ctx.send_message.call_args
        to_addrs = call_args[1].get("to_addrs") or call_args[0][1]
        assert "test@safe.com" in to_addrs
        assert "bank@pnb.in" not in to_addrs

    def test_test_mode_prefixes_subject(self):
        test_env = {**SMTP_ENV, "REQUISITION_TEST_RECIPIENT": "test@safe.com"}
        with patch.dict(os.environ, test_env, clear=False):
            with patch("smtplib.SMTP") as mock_smtp_cls:
                ctx = MagicMock()
                mock_smtp_cls.return_value.__enter__ = lambda s: ctx
                mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
                captured = {}
                def capture_send(msg, **kw):
                    captured["subject"] = msg["Subject"]
                    captured["body"]    = msg.get_content()
                ctx.send_message = capture_send
                mailer_module.send_requisition_email(
                    ["bank@pnb.in"], [], "My Subject", "Body text", "r@x.com"
                )
        assert captured["subject"].startswith("[TEST]")

    def test_test_mode_adds_intended_recipients_line(self):
        test_env = {**SMTP_ENV, "REQUISITION_TEST_RECIPIENT": "test@safe.com"}
        with patch.dict(os.environ, test_env, clear=False):
            with patch("smtplib.SMTP") as mock_smtp_cls:
                ctx = MagicMock()
                mock_smtp_cls.return_value.__enter__ = lambda s: ctx
                mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
                captured = {}
                def capture_send(msg, **kw):
                    captured["body"] = msg.get_content()
                ctx.send_message = capture_send
                mailer_module.send_requisition_email(
                    ["bank@pnb.in", "nodal@pnb.in"], [], "Subj", "Body", "r@x.com"
                )
        assert "bank@pnb.in" in captured["body"]
        assert "Intended recipients" in captured["body"]

    def test_normal_mode_no_prefix(self):
        test_env = {**SMTP_ENV}
        test_env.pop("REQUISITION_TEST_RECIPIENT", None)
        with patch.dict(os.environ, test_env, clear=False):
            with patch("smtplib.SMTP") as mock_smtp_cls:
                ctx = MagicMock()
                mock_smtp_cls.return_value.__enter__ = lambda s: ctx
                mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
                captured = {}
                def capture_send(msg, **kw):
                    captured["subject"] = msg["Subject"]
                ctx.send_message = capture_send
                _, test_mode, _ = mailer_module.send_requisition_email(
                    ["bank@real.com"], [], "Normal Subject", "Body", "r@x.com"
                )
        assert test_mode is False
        assert not captured["subject"].startswith("[TEST]")


# ─────────────────────────────────────────────────────────────────────────────
# 5. No LLM / requests import
# ─────────────────────────────────────────────────────────────────────────────

class TestNoLLMImport:
    def _package_source_files(self) -> list[str]:
        """Return absolute paths of all .py files in services/requisition/."""
        import pathlib
        pkg_dir = pathlib.Path(__file__).parent.parent / "services" / "requisition"
        return [str(p) for p in pkg_dir.rglob("*.py")]

    def test_package_does_not_import_openrouter(self):
        for path in self._package_source_files():
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert "request_openrouter_completion" not in content, (
                f"{path} references request_openrouter_completion"
            )

    def test_package_does_not_import_requests_library(self):
        """Top-level `import requests` is banned (prevents accidental network calls).
        Function-local lazy imports (marked with # noqa: PLC0415) are allowed in
        discovery.py which needs requests only when a search provider is configured.
        """
        import re as _re
        import os as _os
        # Only flag top-level imports (no leading whitespace) that are NOT
        # followed by a noqa comment on the same line.
        pattern = _re.compile(r"^(import requests|from requests\b)(?!.*noqa)", _re.MULTILINE)
        for path in self._package_source_files():
            # discovery.py is explicitly allowed to have function-local lazy imports
            if _os.path.basename(str(path)) == "discovery.py":
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert not pattern.search(content), (
                f"{path} imports the `requests` library at module level"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Central delivery mode
# ─────────────────────────────────────────────────────────────────────────────

class TestCentralDelivery:
    """Tests for REQUISITION_DELIVER_TO (central delivery mode)."""

    def _smtp_ctx(self):
        ctx = MagicMock()
        captured: dict = {}
        def capture_send(msg, **kw):
            captured["msg"]      = msg
            captured["to_addrs"] = kw.get("to_addrs", [])
        ctx.send_message = capture_send
        return ctx, captured

    def _run(self, to, cc, subject, body, extra_env, bank_name="Test Bank"):
        env = {**SMTP_ENV, **extra_env}
        # Clear cache so env changes are picked up
        mailer_module._deliver_to_cache.clear()
        ctx, captured = self._smtp_ctx()
        with patch.dict(os.environ, env, clear=False):
            with patch("smtplib.SMTP") as mock_cls:
                mock_cls.return_value.__enter__ = lambda s: ctx
                mock_cls.return_value.__exit__ = MagicMock(return_value=False)
                result = mailer_module.send_requisition_email(
                    to, cc, subject, body, "officer@police.in", bank_name=bank_name
                )
        return result, captured

    # ── 1. Central delivers ONLY to central address ────────────────────────────
    def test_central_delivers_only_to_central_address(self):
        env = {"REQUISITION_DELIVER_TO": "central@unit.gov.in"}
        (msg_id, test_mode, delivered_to), cap = self._run(
            ["nodal@sbi.co.in"], ["cc@sbi.co.in"], "Subj", "Body", env
        )
        assert test_mode is False
        assert delivered_to == "central@unit.gov.in"
        assert "central@unit.gov.in" in cap["to_addrs"]
        assert "nodal@sbi.co.in" not in cap["to_addrs"]
        assert "cc@sbi.co.in" not in cap["to_addrs"]

    # ── 2. No [TEST] prefix in subject ────────────────────────────────────────
    def test_central_no_test_prefix_in_subject(self):
        env = {"REQUISITION_DELIVER_TO": "central@unit.gov.in"}
        (_, _, _), cap = self._run(
            ["nodal@hdfc.com"], [], "My Subject", "Body", env
        )
        assert cap["msg"]["Subject"] == "My Subject"

    # ── 3. Intended-recipient line in delivered message ────────────────────────
    def test_central_intended_recipient_in_delivered_body(self):
        env = {"REQUISITION_DELIVER_TO": "central@unit.gov.in"}
        (_, _, _), cap = self._run(
            ["nodal@sbi.co.in"], [], "Subj", "Clean body", env, bank_name="State Bank of India"
        )
        delivered_body = cap["msg"].get_content()
        assert "Intended recipient" in delivered_body
        assert "nodal@sbi.co.in" in delivered_body
        assert "central mailbox" in delivered_body
        # Clean body template must also be present
        assert "Clean body" in delivered_body

    # ── 4. Stored body (preview template) is clean (no intended line) ─────────
    def test_central_stored_body_unchanged(self):
        """The body_text passed IN must not be mutated by the mailer."""
        original_body = "This is the clean template."
        env = {"REQUISITION_DELIVER_TO": "central@unit.gov.in"}
        mailer_module._deliver_to_cache.clear()
        with patch.dict(os.environ, {**SMTP_ENV, **env}, clear=False):
            with patch("smtplib.SMTP") as mock_cls:
                ctx, _ = self._smtp_ctx()
                mock_cls.return_value.__enter__ = lambda s: ctx
                mock_cls.return_value.__exit__ = MagicMock(return_value=False)
                mailer_module.send_requisition_email(
                    ["nodal@hdfc.com"], [], "Subj", original_body, "r@x.com"
                )
        # The original string must be unmodified
        assert original_body == "This is the clean template."

    # ── 5. Test mode takes precedence when both set ────────────────────────────
    def test_test_mode_wins_over_central(self):
        env = {
            "REQUISITION_TEST_RECIPIENT": "safety@test.com",
            "REQUISITION_DELIVER_TO": "central@unit.gov.in",
        }
        (msg_id, test_mode, delivered_to), cap = self._run(
            ["nodal@sbi.co.in"], [], "Subj", "Body", env
        )
        assert test_mode is True
        assert delivered_to == "safety@test.com"
        assert cap["msg"]["Subject"].startswith("[TEST]")
        assert "safety@test.com" in cap["to_addrs"]
        assert "central@unit.gov.in" not in cap["to_addrs"]

    # ── 6. Invalid REQUISITION_DELIVER_TO blocks sending ──────────────────────
    def test_invalid_deliver_to_blocks_send(self):
        env = {"REQUISITION_DELIVER_TO": "not-an-email"}
        mailer_module._deliver_to_cache.clear()
        with patch.dict(os.environ, {**SMTP_ENV, **env}, clear=False):
            with pytest.raises(ValueError):
                mailer_module.send_requisition_email(
                    ["nodal@hdfc.com"], [], "Subj", "Body", "r@x.com"
                )

    # ── 7. Direct mode returns delivered_to=None ──────────────────────────────
    def test_direct_mode_delivered_to_is_none(self):
        env: dict = {}
        env.pop("REQUISITION_DELIVER_TO", None)
        env.pop("REQUISITION_TEST_RECIPIENT", None)
        clean_env = {**SMTP_ENV}
        clean_env.pop("REQUISITION_DELIVER_TO", None)
        clean_env.pop("REQUISITION_TEST_RECIPIENT", None)
        mailer_module._deliver_to_cache.clear()
        ctx, _ = self._smtp_ctx()
        with patch.dict(os.environ, clean_env, clear=True):
            with patch("smtplib.SMTP") as mock_cls:
                mock_cls.return_value.__enter__ = lambda s: ctx
                mock_cls.return_value.__exit__ = MagicMock(return_value=False)
                _, test_mode, delivered_to = mailer_module.send_requisition_email(
                    ["nodal@hdfc.com"], [], "Subj", "Body", "r@x.com"
                )
        assert test_mode is False
        assert delivered_to is None

    # ── 8. BCC dropped in central mode ────────────────────────────────────────
    def test_central_bcc_dropped(self):
        env = {
            "REQUISITION_DELIVER_TO": "central@unit.gov.in",
            "REQUISITION_BCC": "archive@police.in",
        }
        (_, _, _), cap = self._run(["nodal@sbi.co.in"], [], "Subj", "Body", env)
        # BCC must not appear in recipients when central mode is active
        assert "archive@police.in" not in cap["to_addrs"]
        assert cap["msg"]["Bcc"] is None
