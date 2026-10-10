"""
services/requisition/bank_names.py
────────────────────────────────────
Normalise a bank name to a stable token-set key used for deduplication and
history look-ups.  Two names are considered the same bank if and only if their
normalised forms are identical (exact token-set equality, never substring).

Examples
--------
    normalize_bank_name("Kotak Mahindra Bank")  == "bank kotak mahindra"
    normalize_bank_name("Bank Kotak Mahindra")  == "bank kotak mahindra"   ← same
    normalize_bank_name("Ratnakar Bank Limited") == "bank ltd ratnakar"
    normalize_bank_name("Ratnakar Bank Ltd")     == "bank ltd ratnakar"    ← same
    normalize_bank_name("Bank of India")         != normalize_bank_name("Bank of Baroda")
"""

import re
import unicodedata


# Legal-word synonyms.  Expand left-hand side → replace with right-hand side
# BEFORE tokenising so that both spellings end up as identical tokens.
_SYNONYMS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\blimited\b",  re.I), "ltd"),
    (re.compile(r"\bprivate\b",  re.I), "pvt"),
    (re.compile(r"\bcompany\b",  re.I), "co"),
    (re.compile(r"\bbank(?:ing)?\b", re.I), "bank"),
    (re.compile(r"\bpayments?\b", re.I), "payments"),
    (re.compile(r"\bfinance\b",  re.I), "finance"),
    (re.compile(r"\bfinancial\b", re.I), "financial"),
]

# Characters to convert to spaces before tokenising.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_bank_name(name: str) -> str:
    """
    Return a stable, sorted token-set string for *name*.

    Steps
    -----
    1. Strip accents / decompose Unicode so ā == a, etc.
    2. Lowercase.
    3. Apply legal-word synonyms (limited → ltd, private → pvt, …).
    4. Replace punctuation with spaces.
    5. Split into tokens, deduplicate, sort.
    6. Join with a single space.

    Token-set equality means "Kotak Mahindra Bank" and "Bank Kotak Mahindra"
    map to the same key, but "Bank of India" and "Bank of Baroda" are different.
    """
    if not name or not name.strip():
        return "__unknown__"

    # 1. Normalise Unicode (decompose accents → strip them)
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))

    # 2. Lowercase
    text = text.lower()

    # 3. Apply synonyms
    for pattern, replacement in _SYNONYMS:
        text = pattern.sub(replacement, text)

    # 4. Replace punctuation with spaces
    text = _PUNCT_RE.sub(" ", text)

    # 5. Tokenise, deduplicate, sort
    tokens = sorted(set(text.split()))

    # 6. Join
    return " ".join(tokens) if tokens else "__unknown__"
