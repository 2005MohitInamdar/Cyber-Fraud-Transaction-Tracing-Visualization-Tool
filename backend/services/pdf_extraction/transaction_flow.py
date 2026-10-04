"""Data-driven reconstruction of NCRP transaction trails.

Reads the mapped JSON files in a directory and writes two audit files:

* ``transaction_flow.json``        nodes, connections, holds and checks (all by id)
* ``no_transaction_records.json``  rows with no flow / no parent, untraced debits,
                                   holds that matched nothing

Source rows are never modified. Identical duplicate rows become one node that
keeps every source row id and every remark.

How a link is decided (layer N -> layer N+1), in this order:
  1. same id (UTR / id inside remarks / Root_UTR)           -> strongest
  2. id off by a few characters AND the amounts agree
  3. receiving bank == child's "action taken by" AND same amount
  4. several children of one bank fit a parent's amount budget (split)
  5. several parents of one bank add up to one child (merge)
  6. child's disputed amount is missing -> bank match only, flagged as estimated
Holds are a status on an account already in the trail, never a hop.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from itertools import combinations, permutations, product
from pathlib import Path
from typing import Any, Callable

ZERO = Decimal("0")

# The only place where input field names are known. A missing field disables
# the rules that need it and is reported in ``checks.missingFieldAliases``.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "layer": ("layer", "layer_no", "layer_number"),
    "bank": ("bank_fi", "bank", "bank_name", "receiving_bank"),
    "action_bank": ("action_taken_by", "action_bank", "sending_bank"),
    "account": ("account_no", "account_number", "account_wallet_id", "wallet_id"),
    "primary_id": ("transaction_id", "utr", "utr_number", "reference_no"),
    "remarks": ("reference_remarks", "remarks", "narration"),
    "transaction_amount": ("transaction_amount", "amount", "amount_pending"),
    "disputed_amount": ("disputed_amount", "fraud_amount"),
    "hold_amount": ("hold_amount", "amount_held", "lien_amount"),
    "description": ("description", "particulars"),
}

_LEGAL_WORDS = {"limited": "ltd", "private": "pvt", "company": "co", "corporation": "corp"}
_SKIP_IN_INITIALS = {"of", "and", "the", "ltd", "pvt", "co", "corp"}
_OUTPUT_FILES = {"transaction_flow.json", "no_transaction_records.json"}
_ROOT_UTR = re.compile(r"root_?utr:?(\d+)", re.I)


@dataclass(frozen=True)
class FlowConfig:
    money_tolerance: Decimal = Decimal("0.01")
    min_id_length: int = 6
    id_max_edits: int = 1                      # "similar" id = at most this many edits
    secondary_id_length_window: int | None = 1  # ids inside remarks must be near the typical id length
    id_length_window: int | None = None         # only used for the outlier warning
    max_subset_size: int = 6
    brute_force_cap: int = 100_000
    bank_fuzzy_similarity: float = 0.92
    confidence_id_exact: float = 1.0
    confidence_id_similar: float = 0.88
    confidence_bank_amount: float = 0.91
    confidence_split_exact: float = 0.86
    confidence_split_partial: float = 0.72
    confidence_merge: float = 0.80
    confidence_unknown_amount: float = 0.50
    confidence_hold_account: float = 0.96
    confidence_hold_utr: float = 0.86
    confidence_hold_bank_amount: float = 0.60
    bank_aliases: dict[str, str] = field(default_factory=dict)  # optional, default EMPTY
    pretty_json: bool = False                   # True = indented (readable), False = compact


@dataclass(frozen=True)
class _Ctx:
    cfg: FlowConfig
    typical_len: int | None


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _value(row: dict[str, Any], kind: str) -> Any:
    for name in FIELD_ALIASES[kind]:
        if row.get(name) not in (None, ""):
            return row[name]
    return None


def _decimal(value: Any) -> Decimal | None:
    """Parse money. Handles 'Rs. 1,234.50' and cells wrapped mid-number ('29,919.3 5')."""
    if value is None or value == "":
        return None
    text = re.sub(r"(?i)\b(?:rs|inr|rupees?)\b\.?|₹", " ", str(value)).replace(",", "")
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    number = re.search(r"-?\d+(?:\.\d+)?", text)
    if not number:
        return None
    try:
        return Decimal(number.group(0))
    except InvalidOperation:
        return None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _same_money(a: Decimal | None, b: Decimal | None, cfg: FlowConfig) -> bool:
    return a is not None and b is not None and abs(a - b) <= cfg.money_tolerance


def _normal_account(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    return (text.lstrip("0") or "0") if text.isdigit() else text


def _digest(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _clean_id(value: Any, cfg: FlowConfig) -> str | None:
    text = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    return text if len(text) >= cfg.min_id_length else None


def _edit_distance(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


# --------------------------------------------------------------------------
# banks: always compare the parent's receiving bank with the child's action_taken_by
# --------------------------------------------------------------------------
def _bank_words(value: Any, cfg: FlowConfig) -> tuple[str, ...]:
    """Ordered, lowercase words with generic legal suffixes unified (limited -> ltd)."""
    words = [_LEGAL_WORDS.get(w, w) for w in re.findall(r"[a-z0-9]+", str(value or "").lower())]
    aliases = {" ".join(re.findall(r"[a-z0-9]+", k.lower())): v for k, v in cfg.bank_aliases.items()}
    alias = aliases.get(" ".join(words))
    return tuple(re.findall(r"[a-z0-9]+", alias.lower())) if alias else tuple(words)


def _is_acronym(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    short, long = sorted((a, b), key=len)
    if len(short) != 1 or len(short[0]) < 3 or len(long) < 2:
        return False
    all_words = "".join(w[0] for w in long)
    content = "".join(w[0] for w in long if w not in _SKIP_IN_INITIALS)
    return short[0] in (all_words, content)


def _bank_match(parent: dict[str, Any], child: dict[str, Any], cfg: FlowConfig) -> tuple[bool, str, float]:
    left, right = parent["bank_words"], child["action_words"]
    if not left or not right:
        return False, "missing_bank", 0.0
    if frozenset(left) == frozenset(right):          # token-set equality: never substring matching
        return True, "bank_token_set", 1.0
    if _is_acronym(left, right):
        return True, "bank_acronym", 0.96
    similarity = SequenceMatcher(None, " ".join(sorted(left)), " ".join(sorted(right))).ratio()
    if similarity >= cfg.bank_fuzzy_similarity:
        return True, "bank_fuzzy", similarity
    return False, "bank_mismatch", similarity


# --------------------------------------------------------------------------
# ids found inside remarks
# --------------------------------------------------------------------------
def _id_sets(remarks: str, ctx: _Ctx) -> tuple[set[str], set[str]]:
    """Return (root_ids, embedded_ids) from a remark string.

    * whitespace is removed first (PDF line wraps split numbers)
    * Root_UTR values are kept apart: they name the FIRST hop, so they are matched only
      against a parent's own id, never against another row's Root_UTR
    * digit runs that touch letters (IFSC fragments), follow "ack" (acknowledgement
      numbers) or are far from the typical id length (phone numbers) are ignored
    """
    cfg = ctx.cfg
    compact = re.sub(r"\s+", "", remarks or "")
    roots = {m for m in _ROOT_UTR.findall(compact) if len(m) >= cfg.min_id_length}
    rest = _ROOT_UTR.sub(" ", compact)
    embedded: set[str] = set()
    for match in re.finditer(r"(?<![A-Za-z0-9])\d+", rest):
        run = match.group(0)
        if len(run) < cfg.min_id_length:
            continue
        if "ack" in rest[max(0, match.start() - 10):match.start()].lower():
            continue
        if ctx.typical_len and cfg.secondary_id_length_window is not None \
                and abs(len(run) - ctx.typical_len) > cfg.secondary_id_length_window:
            continue
        embedded.add(run)
    return roots, embedded


# --------------------------------------------------------------------------
# nodes (one per distinct transaction) and holds (one per source row)
# --------------------------------------------------------------------------
def _group_rows(items: list[tuple[str, int, dict[str, Any]]], ctx: _Ctx) -> list[dict[str, Any]]:
    cfg = ctx.cfg
    groups: dict[tuple[Any, ...], list[tuple[str, dict[str, Any]]]] = {}
    for table, layer, row in items:
        key = (layer, tuple(sorted(set(_bank_words(_value(row, "bank"), cfg)))),
               _normal_account(_value(row, "account")), _clean_id(_value(row, "primary_id"), cfg),
               str(_decimal(_value(row, "transaction_amount"))), str(_decimal(_value(row, "disputed_amount"))))
        groups.setdefault(key, []).append((table, row))
    nodes = []
    for key, members in groups.items():
        members.sort(key=lambda member: _digest(member[1]))
        nodes.append(_build_node(key, members, ctx))
    return sorted(nodes, key=lambda node: node["id"])


def _build_node(key: tuple[Any, ...], members: list[tuple[str, dict[str, Any]]], ctx: _Ctx) -> dict[str, Any]:
    cfg, layer = ctx.cfg, key[0]
    rows = [row for _, row in members]

    def pick(kind: str) -> Any:                      # first non-empty value across duplicate rows
        for row in rows:
            if _value(row, kind) is not None:
                return _value(row, kind)
        return None

    remarks = " | ".join(dict.fromkeys(str(v) for v in (_value(r, "remarks") for r in rows) if v))
    tx, disputed = _decimal(pick("transaction_amount")), _decimal(pick("disputed_amount"))
    primary = _clean_id(pick("primary_id"), cfg)
    roots, embedded = _id_sets(remarks, ctx)
    return {
        "id": "node:" + _digest(key), "table": members[0][0], "layer": layer,
        "sourceRowIds": [f"{table}:{_digest(row)}" for table, row in members],
        "bank": pick("bank"), "action_bank": pick("action_bank"), "account": pick("account"),
        "account_norm": _normal_account(pick("account")),
        "bank_words": _bank_words(pick("bank"), cfg), "action_words": _bank_words(pick("action_bank"), cfg),
        "tx": tx, "disputed": disputed,
        # effective amount: the disputed part, or the full amount when disputed is missing
        "amount": disputed if disputed is not None else tx,
        "estimated": layer > 0 and disputed is None and tx is not None,
        "primary": primary, "secondary": embedded, "roots": roots,
        "ids": embedded | ({primary} if primary else set()),
        "remarks": remarks, "frozen": ZERO, "hold_ids": [],
    }


def _build_hold(table: str, row: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    remarks = str(_value(row, "remarks") or "")
    roots, embedded = _id_sets(remarks, ctx)
    return {
        "id": "hold:" + _digest((table, row)), "sourceRowIds": [f"{table}:{_digest(row)}"],
        "account": _value(row, "account"), "account_norm": _normal_account(_value(row, "account")),
        "amount": _decimal(_value(row, "hold_amount")), "date": row.get("hold_date"),
        "remarks": remarks, "action_bank": _value(row, "action_bank"),
        "action_words": _bank_words(_value(row, "action_bank"), ctx.cfg), "ids": embedded | roots,
    }


# --------------------------------------------------------------------------
# input discovery (by content, not file name)
# --------------------------------------------------------------------------
def _payloads(directory: Path) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    rows_payloads, kv_payloads = [], []
    for path in sorted(directory.glob("*.json")):
        if path.name in _OUTPUT_FILES:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        table = str(data.get("table") or path.stem)
        if isinstance(data.get("rows"), list):
            rows_payloads.append((table, data))
        elif isinstance(data.get("data"), dict):
            kv_payloads.append((table, data["data"]))
    return rows_payloads, kv_payloads


def _fields(payload: dict[str, Any]) -> set[str]:
    return {name for row in payload.get("rows", []) if isinstance(row, dict) for name in row}


def _is_hold(table: str, payload: dict[str, Any]) -> bool:
    return "hold" in table.lower() or bool(set(FIELD_ALIASES["hold_amount"]) & _fields(payload))


def _is_trail(_table: str, payload: dict[str, Any]) -> bool:
    fields = _fields(payload)
    return bool(set(FIELD_ALIASES["layer"]) & fields and set(FIELD_ALIASES["action_bank"]) & fields)


def _is_base(table: str, payload: dict[str, Any]) -> bool:
    fields = _fields(payload)
    return (not _is_hold(table, payload) and not _is_trail(table, payload)
            and bool(set(FIELD_ALIASES["primary_id"]) & fields)
            and bool(set(FIELD_ALIASES["transaction_amount"]) & fields))


# --------------------------------------------------------------------------
# linking
# --------------------------------------------------------------------------
class _State:
    def __init__(self) -> None:
        self.connections: list[dict[str, Any]] = []
        self.parent_of: dict[str, list[str]] = defaultdict(list)
        self.children_of: dict[str, list[str]] = defaultdict(list)
        self.used: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self.rejected: list[dict[str, Any]] = []

    def add(self, parent: dict[str, Any], child: dict[str, Any], rule: str, confidence: float, *,
            ambiguous: bool = False, merged: bool = False, amount: Decimal | None = None,
            estimated: bool | None = None) -> None:
        passed = amount if amount is not None else child["amount"]
        if passed is not None:
            self.used[parent["id"]] += passed
        self.parent_of[child["id"]].append(parent["id"])
        self.children_of[parent["id"]].append(child["id"])
        self.connections.append({
            "from": parent["id"], "to": [child["id"]], "matchRule": rule, "confidence": confidence,
            "amountPassed": _s(passed), "ambiguous": ambiguous, "merged": merged,
            "amountEstimated": child["estimated"] if estimated is None else estimated,
        })


def _available(node: dict[str, Any], used: dict[str, Decimal]) -> Decimal | None:
    return None if node["amount"] is None else node["amount"] - used[node["id"]]


def _can_assign(parent: dict[str, Any], child: dict[str, Any], used: dict[str, Decimal], cfg: FlowConfig) -> bool:
    budget = _available(parent, used)
    return child["amount"] is None or budget is None or child["amount"] <= budget + cfg.money_tolerance


def _exact_id_rule(parent: dict[str, Any], child: dict[str, Any]) -> str | None:
    if parent["ids"] & child["ids"]:
        return "transaction_id_exact"
    if parent["primary"] and parent["primary"] in child["roots"]:
        return "root_utr_exact"
    return None


def _similar_distance(parent: dict[str, Any], child: dict[str, Any], cfg: FlowConfig) -> int | None:
    best = None
    for a in parent["ids"]:
        for b in child["ids"]:
            distance = _edit_distance(a, b, cfg.id_max_edits)
            if 0 < distance <= cfg.id_max_edits:
                best = distance if best is None else min(best, distance)
    return best


def _split_assign(parents: list[dict[str, Any]], children: list[dict[str, Any]],
                  used: dict[str, Decimal], cfg: FlowConfig) -> list[tuple[dict[str, Any], dict[str, Any], bool]]:
    """Give every child to one parent (or nobody) so the explained amount is maximal
    and no parent exceeds its remaining budget. Equal-best answers are flagged ambiguous."""
    possible = [[i for i, p in enumerate(parents) if _bank_match(p, c, cfg)[0]] for c in children]
    budgets = [_available(p, used) for p in parents]          # None = unknown = unlimited
    amounts = [c["amount"] for c in children]

    def score(option: tuple[int | None, ...]) -> Decimal | None:
        spent, total = [ZERO] * len(parents), ZERO
        for ci, pi in enumerate(option):
            if pi is None:
                continue
            if amounts[ci] is None:
                return None
            spent[pi] += amounts[ci]
            total += amounts[ci]
        if any(b is not None and spent[i] > b + cfg.money_tolerance for i, b in enumerate(budgets)):
            return None
        return total

    states = 1
    for options in possible:
        states *= len(options) + 1
    best_score, best = Decimal("-1"), []
    ambiguous = False
    if states <= cfg.brute_force_cap:
        for option in product(*[[None, *options] for options in possible]):
            value = score(option)
            if value is None:
                continue
            if value > best_score:
                best_score, best = value, [option]
            elif value == best_score:
                best.append(option)
        ambiguous = len(best) > 1
        chosen = best[0] if best else tuple(None for _ in children)
    else:                                                     # greedy safety net for huge groups
        left, chosen_list = list(budgets), [None] * len(children)
        for ci in sorted(range(len(children)), key=lambda i: (-(amounts[i] or ZERO), children[i]["id"])):
            for pi in sorted(possible[ci], key=lambda i: parents[i]["id"]):
                if amounts[ci] is not None and (left[pi] is None or amounts[ci] <= left[pi] + cfg.money_tolerance):
                    chosen_list[ci] = pi
                    left[pi] = None if left[pi] is None else left[pi] - amounts[ci]
                    break
        chosen, ambiguous = tuple(chosen_list), True
    return [(parents[pi], children[ci], ambiguous) for ci, pi in enumerate(chosen) if pi is not None]


def _link_layers(parents: list[dict[str, Any]], children: list[dict[str, Any]], st: _State, ctx: _Ctx) -> None:
    cfg = ctx.cfg

    # 1. same id (exact), preferring a parent whose bank also agrees
    for child in children:
        found = [(p, rule) for p in parents if (rule := _exact_id_rule(p, child)) and _can_assign(p, child, st.used, cfg)]
        if found:
            parent, rule = min(found, key=lambda item: (not _bank_match(item[0], child, cfg)[0], item[0]["id"]))
            st.add(parent, child, rule, cfg.confidence_id_exact, ambiguous=len(found) > 1)

    # 2. similar id (few edits) -- accepted only when the amounts also agree
    for child in children:
        if st.parent_of[child["id"]]:
            continue
        found = []
        for parent in parents:
            distance = _similar_distance(parent, child, cfg)
            if distance is None:
                continue
            if not _same_money(parent["amount"], child["amount"], cfg):
                st.rejected.append({"from": parent["id"], "to": child["id"],
                                    "reason": "similar_id_without_amount_agreement"})
            elif _can_assign(parent, child, st.used, cfg):
                found.append((distance, parent))
        if found:
            distance, parent = min(found, key=lambda item: (item[0], item[1]["id"]))
            st.add(parent, child, "transaction_id_similar", cfg.confidence_id_similar, ambiguous=len(found) > 1)

    # 3. bank + same amount
    for child in children:
        if st.parent_of[child["id"]]:
            continue
        found = [p for p in parents if _bank_match(p, child, cfg)[0]
                 and _same_money(p["amount"], child["amount"], cfg) and _can_assign(p, child, st.used, cfg)]
        if found:
            st.add(min(found, key=lambda p: p["id"]), child, "bank_and_amount", cfg.confidence_bank_amount,
                   ambiguous=len(found) > 1)

    # 4. split: children of one bank share parents' remaining budgets
    remaining = [c for c in children if not st.parent_of[c["id"]]]
    if remaining:
        budgets_before = {p["id"]: _available(p, st.used) for p in parents}
        plan = _split_assign(parents, remaining, st.used, cfg)
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for parent, child, _ in plan:
            totals[parent["id"]] += child["amount"]
        for parent, child, ambiguous in plan:
            exact = _same_money(totals[parent["id"]], budgets_before[parent["id"]], cfg)
            st.add(parent, child, "bank_split_amount",
                   cfg.confidence_split_exact if exact else cfg.confidence_split_partial, ambiguous=ambiguous)

    # 5. merge: several parents of one bank add up to one still-parentless child
    for child in children:
        if st.parent_of[child["id"]] or child["amount"] is None or child["estimated"]:
            continue
        options = [p for p in parents if _bank_match(p, child, cfg)[0] and (_available(p, st.used) or ZERO) > 0]
        groups = [g for size in range(2, min(cfg.max_subset_size, len(options)) + 1)
                  for g in combinations(options, size)
                  if _same_money(sum((_available(p, st.used) or ZERO for p in g), ZERO), child["amount"], cfg)]
        if groups:
            group = min(groups, key=lambda g: tuple(p["id"] for p in g))
            for parent in group:    # each parent gives only what it still has
                st.add(parent, child, "multiple_parents_merge", cfg.confidence_merge,
                       ambiguous=len(groups) > 1, merged=True, amount=_available(parent, st.used))

    # 6. disputed amount missing: bank match only, amount is an estimate
    for child in children:
        if st.parent_of[child["id"]] or not child["estimated"]:
            continue
        found = [(avail, p) for p in parents if _bank_match(p, child, cfg)[0]
                 and (avail := _available(p, st.used)) is not None and avail > cfg.money_tolerance]
        if found:
            avail, parent = min(found, key=lambda item: (-item[0], item[1]["id"]))
            st.add(parent, child, "bank_unknown_amount", cfg.confidence_unknown_amount,
                   ambiguous=len(found) > 1, amount=min(child["amount"], avail), estimated=True)


# --------------------------------------------------------------------------
# holds
# --------------------------------------------------------------------------
def _assign_account_group(holds: list[dict[str, Any]], nodes: list[dict[str, Any]],
                          cfg: FlowConfig) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Several holds can share one account: give each hold its own row where possible,
    preferring the row whose bank placed the hold and whose amount is closest."""
    def cost(hold: dict[str, Any], node: dict[str, Any]) -> tuple[int, Decimal]:
        mismatch = 0 if _bank_match(node, hold, cfg)[0] else 1
        return mismatch, abs((hold["amount"] or ZERO) - (node["amount"] if node["amount"] is not None else ZERO))

    holds = sorted(holds, key=lambda h: (h["amount"] or ZERO, h["id"]))
    nodes = sorted(nodes, key=lambda n: n["id"])
    if len(holds) <= len(nodes) <= 7:
        best, best_key = None, None
        for perm in permutations(nodes, len(holds)):
            costs = [cost(h, n) for h, n in zip(holds, perm)]
            key = (sum(c[0] for c in costs), sum(c[1] for c in costs), tuple(n["id"] for n in perm))
            if best_key is None or key < best_key:
                best, best_key = perm, key
        return list(zip(holds, best))
    taken: set[str] = set()
    pairs = []
    for hold in holds:
        node = min(nodes, key=lambda n: (n["id"] in taken, *cost(hold, n), n["id"]))
        taken.add(node["id"])
        pairs.append((hold, node))
    return pairs


def _attach_holds(trail_nodes: list[dict[str, Any]], holds: list[dict[str, Any]],
                  cfg: FlowConfig) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []

    def attach(hold: dict[str, Any], node: dict[str, Any], rule: str, confidence: float) -> None:
        node["frozen"] += hold["amount"] or ZERO
        node["hold_ids"].append(hold["id"])
        links.append({"hold": hold["id"], "node": node["id"], "matchRule": rule,
                      "confidence": confidence, "amount": _s(hold["amount"])})

    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    leftovers = []
    for hold in holds:
        (by_account[hold["account_norm"]] if hold["account_norm"] else leftovers).append(hold)
    for account, group in sorted(by_account.items()):
        candidates = [n for n in trail_nodes if n["account_norm"] == account]
        if candidates:
            for hold, node in _assign_account_group(group, candidates, cfg):
                attach(hold, node, "account", cfg.confidence_hold_account)
        else:
            leftovers.extend(group)
    for hold in sorted(leftovers, key=lambda h: h["id"]):
        by_id = [n for n in trail_nodes if hold["ids"] & (n["ids"] | n["roots"])]
        if by_id:
            attach(hold, min(by_id, key=lambda n: (not _bank_match(n, hold, cfg)[0], n["id"])),
                   "utr_in_hold_remarks", cfg.confidence_hold_utr)
            continue
        if hold["amount"] is not None:
            by_bank = [n for n in trail_nodes if _bank_match(n, hold, cfg)[0]
                       and (n["tx"] if n["tx"] is not None else Decimal("-1")) + cfg.money_tolerance >= hold["amount"]]
            if by_bank:
                attach(hold, min(by_bank, key=lambda n: (abs(n["amount"] - hold["amount"]) if n["amount"] is not None
                                                          else Decimal("1E9"), n["id"])),
                       "bank_and_partial_hold_amount", cfg.confidence_hold_bank_amount)
    return links


# --------------------------------------------------------------------------
# reported totals, checks, output
# --------------------------------------------------------------------------
def _reported_totals(rows_payloads: list[tuple[str, dict[str, Any]]],
                     kv_payloads: list[tuple[str, dict[str, Any]]]) -> tuple[Decimal | None, Decimal | None]:
    fraud = lien = None
    for _table, payload in rows_payloads:
        for row in payload.get("rows", []):
            if not isinstance(row, dict):
                continue
            text, amount = str(_value(row, "description") or "").lower(), _decimal(_value(row, "transaction_amount"))
            if amount is None:
                continue
            if fraud is None and any(w in text for w in ("fraud", "disputed", "complaint")):
                fraud = amount
            if lien is None and any(w in text for w in ("lien", "hold", "frozen")):
                lien = amount
    for _table, data in kv_payloads:                     # e.g. {"total_fraudulent_amount": "50,099.00"}
        for key, raw in data.items():
            name, amount = str(key).lower(), _decimal(raw)
            if amount is None:
                continue
            if fraud is None and "fraud" in name:
                fraud = amount
            if lien is None and ("lien" in name or "hold" in name):
                lien = amount
    return fraud, lien


def _unparsed_cells(rows_payloads: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for table, payload in rows_payloads:
        for row in payload["rows"]:
            if not isinstance(row, dict):
                continue
            for kind in ("transaction_amount", "disputed_amount", "hold_amount"):
                for name in FIELD_ALIASES[kind]:
                    raw = row.get(name)
                    if raw not in (None, "") and _decimal(raw) is None:
                        out.append({"table": table, "s_no": row.get("s_no"), "field": name, "value": str(raw)})
    return out


def _node_out(node: dict[str, Any], used: dict[str, Decimal]) -> dict[str, Any]:
    return {
        "id": node["id"], "table": node["table"], "layer": node["layer"], "sourceRowIds": node["sourceRowIds"],
        "transactionId": node["primary"], "embeddedIds": sorted(node["secondary"]), "rootIds": sorted(node["roots"]),
        "bank": node["bank"], "actionTakenBy": node["action_bank"], "account": node["account"],
        "transactionAmount": _s(node["tx"]), "disputedAmount": _s(node["disputed"]),
        "amountEstimated": node["estimated"], "frozenAmount": str(node["frozen"]),
        "unaccountedAmount": _s(_available(node, used)), "holdIds": node["hold_ids"], "remarks": node["remarks"],
    }


def _brief(node: dict[str, Any]) -> dict[str, Any]:
    return {"id": node["id"], "layer": node["layer"], "transactionId": node["primary"], "bank": node["bank"],
            "account": node["account"], "transactionAmount": _s(node["tx"]), "disputedAmount": _s(node["disputed"])}


def _dump(path: Path, data: Any, pretty: bool) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) if pretty \
        else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")


def resolve(flow: dict[str, Any], identifier: str) -> dict[str, Any] | None:
    """Look an id up in transaction_flow.json (node or hold)."""
    return flow["nodes"].get(identifier) or flow["holds"].get(identifier)


def build_transaction_flow(output_dir: str | Path, progress: Callable[[str], None] | None = None,
                           config: FlowConfig | None = None) -> dict[str, Any]:
    """Build ``transaction_flow.json`` and ``no_transaction_records.json`` from mapped JSON files."""
    cfg, directory = config or FlowConfig(), Path(output_dir)
    rows_payloads, kv_payloads = _payloads(directory)
    hold_payloads = [(t, p) for t, p in rows_payloads if _is_hold(t, p)]
    trail_payloads = [(t, p) for t, p in rows_payloads if _is_trail(t, p)]
    base_payloads = [(t, p) for t, p in rows_payloads if _is_base(t, p)]

    base_items = [(t, 0, row) for t, p in base_payloads for row in p["rows"] if isinstance(row, dict)]
    trail_items = []
    for table, payload in trail_payloads:
        for row in payload["rows"]:
            try:
                layer = int(_value(row, "layer"))
            except (TypeError, ValueError):
                continue
            if layer > 0:
                trail_items.append((table, layer, row))

    lengths = Counter(len(pid) for _t, _l, row in base_items + trail_items
                      if (pid := _clean_id(_value(row, "primary_id"), cfg)))
    typical = max(lengths.items(), key=lambda kv: (kv[1], kv[0]))[0] if lengths else None
    ctx = _Ctx(cfg, typical)

    nodes = _group_rows(base_items + trail_items, ctx)
    trail_nodes = [n for n in nodes if n["layer"] > 0]
    base_nodes = [n for n in nodes if n["layer"] == 0]
    holds = [_build_hold(t, row, ctx) for t, p in hold_payloads for row in p["rows"] if isinstance(row, dict)]

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        grouped[node["layer"]].append(node)
    layers = sorted(grouped)

    st = _State()
    for previous, current in zip(layers, layers[1:]):
        if progress:
            progress(f"Matching transaction layer {previous} to layer {current}.")
        _link_layers(grouped[previous], grouped[current], st, ctx)

    hold_links = _attach_holds(trail_nodes, holds, cfg)
    attached = {link["hold"] for link in hold_links}

    no_flow = [n for n in nodes if not st.parent_of[n["id"]] and not st.children_of[n["id"]] and not n["hold_ids"]]
    unlinked = [n for n in trail_nodes if not st.parent_of[n["id"]]]
    untraced = [n for n in base_nodes if not st.children_of[n["id"]]]
    end_of_trail = [n["id"] for n in trail_nodes if st.parent_of[n["id"]] and not st.children_of[n["id"]]]

    base_total = sum((n["amount"] or ZERO for n in base_nodes), ZERO)
    hold_total = sum((h["amount"] or ZERO for h in holds), ZERO)
    fraud_total, lien_total = _reported_totals(rows_payloads, kv_payloads)
    merged_children = {c["to"][0] for c in st.connections if c["merged"]}
    outliers = [n["id"] for n in nodes if typical and cfg.id_length_window is not None and n["primary"]
                and abs(len(n["primary"]) - typical) > cfg.id_length_window]
    checks = {
        "missingFieldAliases": sorted(a for a, names in FIELD_ALIASES.items()
                                      if not any(set(names) & _fields(p) for _t, p in rows_payloads)),
        "baseDebitTotal": str(base_total), "reportedFraudTotal": _s(fraud_total),
        "baseDebitsMatchReportedFraud": None if fraud_total is None else _same_money(base_total, fraud_total, cfg),
        "holdTotal": str(hold_total), "reportedLienTotal": _s(lien_total),
        "holdsMatchReportedLien": None if lien_total is None else _same_money(hold_total, lien_total, cfg),
        "holdsAttached": len(attached), "holdsTotal": len(holds),
        "childrenWithMultipleParents": [c for c, ps in st.parent_of.items() if len(ps) > 1 and c not in merged_children],
        "parentBudgetViolations": [n["id"] for n in nodes
                                   if (a := _available(n, st.used)) is not None and a < -cfg.money_tolerance],
        "rowsMissingDisputedAmount": [n["id"] for n in trail_nodes if n["estimated"]],
        "unparsedAmountCells": _unparsed_cells(rows_payloads),
        "typicalIdLength": typical, "idLengthOutliers": outliers,
    }
    flow = {
        "layers": layers, "connectionCount": len(st.connections), "connections": st.connections,
        "nodes": {n["id"]: _node_out(n, st.used) for n in nodes},
        "holds": {h["id"]: {"id": h["id"], "sourceRowIds": h["sourceRowIds"], "account": h["account"],
                            "holdAmount": _s(h["amount"]), "holdDate": h["date"], "remarks": h["remarks"],
                            "actionTakenBy": h["action_bank"], "embeddedIds": sorted(h["ids"])} for h in holds},
        "holdLinks": hold_links, "endOfTrail": end_of_trail, "rejectedCandidates": st.rejected,
        "checks": checks, "unmatchedCount": len(no_flow),
    }
    no_records = {
        "noFlowRecords": [_brief(n) for n in no_flow],
        "unlinkedRows": [_brief(n) for n in unlinked],
        "untracedBase": [_brief(n) for n in untraced],
        "unmatchedHolds": [h["id"] for h in holds if h["id"] not in attached],
    }
    flow_path, no_path = directory / "transaction_flow.json", directory / "no_transaction_records.json"
    _dump(flow_path, flow, cfg.pretty_json)
    _dump(no_path, no_records, cfg.pretty_json)
    if progress:
        progress(f"Built {len(st.connections)} flow connection(s); {len(no_flow)} record(s) have no flow.")
    return {"flowPath": str(flow_path), "noFlowPath": str(no_path),
            "connectionCount": len(st.connections), "unmatchedCount": len(no_flow)}