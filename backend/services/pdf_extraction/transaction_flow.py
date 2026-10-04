# """Data-driven reconstruction of NCRP transaction trails.

# The builder reads mapped JSON only and writes an auditable graph.  Source rows
# are never changed; duplicate rows become one node retaining all source IDs.
# """
# from __future__ import annotations

# import hashlib
# import json
# import re
# from collections import Counter, defaultdict
# from dataclasses import dataclass, field
# from decimal import Decimal, InvalidOperation
# from difflib import SequenceMatcher
# from itertools import combinations, product
# from pathlib import Path
# from typing import Any, Callable


# # The only place where input field names are known. Missing fields merely
# # disable the applicable rule and are reported in ``checks``.
# FIELD_ALIASES: dict[str, tuple[str, ...]] = {
#     "layer": ("layer", "layer_no", "layer_number"),
#     "bank": ("bank_fi", "bank", "bank_name", "receiving_bank"),
#     "action_bank": ("action_taken_by", "action_bank", "sending_bank"),
#     "account": ("account_no", "account_number", "account_wallet_id", "wallet_id"),
#     "primary_id": ("transaction_id", "utr", "utr_number", "reference_no"),
#     "remarks": ("reference_remarks", "remarks", "narration"),
#     "transaction_amount": ("transaction_amount", "amount", "amount_pending"),
#     "disputed_amount": ("disputed_amount", "fraud_amount"),
#     "hold_amount": ("hold_amount", "amount_held", "lien_amount"),
#     "description": ("description", "particulars"),
# }


# @dataclass(frozen=True)
# class FlowConfig:
#     money_tolerance: Decimal = Decimal("0.01")
#     id_similarity: float = 0.88
#     min_id_length: int = 6
#     max_subset_size: int = 6
#     brute_force_cap: int = 100_000
#     id_length_window: int | None = None
#     bank_fuzzy_similarity: float = 0.92
#     confidence_id_exact: float = 1.0
#     confidence_id_similar: float = 0.88
#     confidence_bank_amount: float = 0.91
#     confidence_split_exact: float = 0.86
#     confidence_split_partial: float = 0.72
#     confidence_merge: float = 0.80
#     confidence_hold_account: float = 0.96
#     confidence_hold_utr: float = 0.86
#     confidence_hold_bank_amount: float = 0.60
#     bank_aliases: dict[str, str] = field(default_factory=dict)


# def _value(row: dict[str, Any], kind: str) -> Any:
#     for name in FIELD_ALIASES[kind]:
#         if row.get(name) not in (None, ""):
#             return row[name]
#     return None


# def _decimal(value: Any) -> Decimal | None:
#     if value is None or value == "":
#         return None
#     clean = re.sub(r"(?i)\b(?:rs|inr|rupees?)\.?\b|₹", "", str(value)).replace(",", "")
#     number = re.search(r"-?\d+(?:\.\d+)?", clean)
#     if not number:
#         return None
#     try:
#         return Decimal(number.group(0))
#     except InvalidOperation:
#         return None


# def _money(record: dict[str, Any]) -> Decimal | None:
#     return _decimal(_value(record["row"], "disputed_amount")) or _decimal(_value(record["row"], "transaction_amount"))


# def _actual_money(record: dict[str, Any]) -> Decimal | None:
#     return _decimal(_value(record["row"], "transaction_amount"))


# def _same_money(a: Decimal | None, b: Decimal | None, cfg: FlowConfig) -> bool:
#     return a is not None and b is not None and abs(a - b) <= cfg.money_tolerance


# def _normal_account(value: Any) -> str:
#     text = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
#     return text.lstrip("0") or "0" if text.isdigit() else text


# def _bank_tokens(value: Any, cfg: FlowConfig) -> frozenset[str]:
#     raw = " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))
#     raw = cfg.bank_aliases.get(raw, raw)
#     return frozenset(re.findall(r"[a-z0-9]+", raw))


# def _bank_match(parent: dict[str, Any], child: dict[str, Any], cfg: FlowConfig) -> tuple[bool, str, float]:
#     """Compare receiving bank to the child's action-taken-by parent pointer."""
#     left = _bank_tokens(_value(parent["row"], "bank"), cfg)
#     right = _bank_tokens(_value(child["row"], "action_bank"), cfg)
#     if not left or not right:
#         return False, "missing_bank", 0.0
#     if left == right:
#         return True, "bank_token_set", 1.0
#     short, long = sorted((left, right), key=len)
#     if len(short) == 1 and next(iter(short)) == "".join(word[0] for word in long):
#         return True, "bank_acronym", 0.96
#     similarity = SequenceMatcher(None, " ".join(sorted(left)), " ".join(sorted(right))).ratio()
#     return (True, "bank_fuzzy", similarity) if similarity >= cfg.bank_fuzzy_similarity else (False, "bank_mismatch", similarity)


# def _id_values(record: dict[str, Any], cfg: FlowConfig) -> tuple[str | None, set[str]]:
#     primary = re.sub(r"[^a-z0-9]", "", str(_value(record["row"], "primary_id") or "").lower())
#     primary = primary if len(primary) >= cfg.min_id_length else None
#     # Remarks are never used as a whole ID: only digit runs, after whitespace
#     # removal, are treated as secondary identifiers (including Root_UTR).
#     compact = re.sub(r"\s+", "", str(_value(record["row"], "remarks") or ""))
#     secondary = {item for item in re.findall(r"\d+", compact) if len(item) >= cfg.min_id_length}
#     return primary, secondary


# def _id_match(parent: dict[str, Any], child: dict[str, Any], cfg: FlowConfig) -> tuple[str | None, float]:
#     primary_a, secondary_a = _id_values(parent, cfg)
#     primary_b, secondary_b = _id_values(child, cfg)
#     values_a = secondary_a | ({primary_a} if primary_a else set())
#     values_b = secondary_b | ({primary_b} if primary_b else set())
#     if values_a & values_b:
#         return "transaction_id_exact", 1.0
#     if not values_a or not values_b:
#         return None, 0.0
#     similarity = max(SequenceMatcher(None, a, b).ratio() for a in values_a for b in values_b)
#     return ("transaction_id_similar", similarity) if similarity >= cfg.id_similarity else (None, similarity)


# def _digest(value: Any) -> str:
#     return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


# def _make_nodes(table: str, rows: list[dict[str, Any]], layer: int, cfg: FlowConfig) -> list[dict[str, Any]]:
#     merged: dict[tuple[Any, ...], dict[str, Any]] = {}
#     for row in rows:
#         if not isinstance(row, dict):
#             continue
#         probe = {"table": table, "layer": layer, "row": dict(row), "config": cfg}
#         primary, _ = _id_values(probe, cfg)
#         key = (layer, " ".join(sorted(_bank_tokens(_value(row, "bank"), cfg))), _normal_account(_value(row, "account")), primary, str(_actual_money(probe) or ""), str(_decimal(_value(row, "disputed_amount")) or ""))
#         if key not in merged:
#             merged[key] = {**probe, "id": "node:" + _digest(key), "sourceRowIds": []}
#         merged[key]["sourceRowIds"].append(f"{table}:{_digest(row)}")
#     for node in merged.values():
#         node["sourceRowIds"].sort()
#     return sorted(merged.values(), key=lambda node: node["id"])


# def _ref(node: dict[str, Any]) -> dict[str, Any]:
#     primary, secondary = _id_values(node, node["config"])
#     return {
#         "id": node["id"], "table": node["table"], "layer": node["layer"], "sourceRowIds": node["sourceRowIds"],
#         "transactionId": primary, "embeddedIds": sorted(secondary), "bank": _value(node["row"], "bank"),
#         "actionTakenBy": _value(node["row"], "action_bank"), "account": _value(node["row"], "account"),
#         "transactionAmount": _value(node["row"], "transaction_amount"), "disputedAmount": _value(node["row"], "disputed_amount"),
#     }


# def _payloads(directory: Path) -> list[tuple[str, dict[str, Any]]]:
#     output = []
#     for path in sorted(directory.glob("*.json")):
#         if path.name in {"transaction_flow.json", "no_transaction_records.json"}:
#             continue
#         try:
#             data = json.loads(path.read_text(encoding="utf-8"))
#         except (OSError, json.JSONDecodeError):
#             continue
#         if isinstance(data, dict) and isinstance(data.get("rows"), list):
#             output.append((str(data.get("table") or path.stem), data))
#     return output


# def _fields(payload: dict[str, Any]) -> set[str]:
#     return {name for row in payload.get("rows", []) if isinstance(row, dict) for name in row}


# def _is_hold(table: str, payload: dict[str, Any]) -> bool:
#     return "hold" in table.lower() or bool(set(FIELD_ALIASES["hold_amount"]) & _fields(payload))


# def _is_trail(_table: str, payload: dict[str, Any]) -> bool:
#     fields = _fields(payload)
#     return bool(set(FIELD_ALIASES["layer"]) & fields and set(FIELD_ALIASES["action_bank"]) & fields)


# def _is_base(table: str, payload: dict[str, Any]) -> bool:
#     fields = _fields(payload)
#     return not _is_hold(table, payload) and not _is_trail(table, payload) and bool(set(FIELD_ALIASES["primary_id"]) & fields) and bool(set(FIELD_ALIASES["transaction_amount"]) & fields)


# def _available(parent: dict[str, Any], used: dict[str, Decimal]) -> Decimal | None:
#     amount = _money(parent)
#     return amount - used.get(parent["id"], Decimal("0")) if amount is not None else None


# def _can_assign(parent: dict[str, Any], child: dict[str, Any], used: dict[str, Decimal], cfg: FlowConfig) -> bool:
#     amount, budget = _money(child), _available(parent, used)
#     return amount is None or budget is None or amount <= budget + cfg.money_tolerance


# def _add_connection(connections: list[dict[str, Any]], parent: dict[str, Any], child: dict[str, Any], rule: str, confidence: float, used: dict[str, Decimal], parent_of: dict[str, list[str]], children_of: dict[str, list[str]], ambiguous: bool = False, merged: bool = False, amount_override: Decimal | None = None) -> None:
#     amount = amount_override if amount_override is not None else _money(child)
#     if amount is not None:
#         used[parent["id"]] += amount
#     parent_of[child["id"]].append(parent["id"]); children_of[parent["id"]].append(child["id"])
#     connections.append({"from": _ref(parent), "to": [_ref(child)], "matchRule": rule, "confidence": confidence, "amountPassed": str(amount) if amount is not None else None, "ambiguous": ambiguous, "merged": merged})


# def _split_assign(parents: list[dict[str, Any]], children: list[dict[str, Any]], used: dict[str, Decimal], cfg: FlowConfig) -> list[tuple[dict[str, Any], dict[str, Any], bool]]:
#     """Maximise explained child amount, keeping every child to one parent."""
#     possible = [[parent for parent in parents if _bank_match(parent, child, cfg)[0]] for child in children]
#     states = 1
#     for options in possible:
#         states *= len(options) + 1
#     best_score, best_options = Decimal("-1"), []
#     def consider(option: tuple[int | None, ...]) -> None:
#         nonlocal best_score, best_options
#         local, score = dict(used), Decimal("0")
#         for index, parent_index in enumerate(option):
#             if parent_index is None:
#                 continue
#             parent, child, amount = parents[parent_index], children[index], _money(children[index])
#             budget = _available(parent, local)
#             if amount is None or budget is None or amount > budget + cfg.money_tolerance:
#                 return
#             local[parent["id"]] = local.get(parent["id"], Decimal("0")) + amount; score += amount
#         if score > best_score:
#             best_score, best_options = score, [option]
#         elif score == best_score:
#             best_options.append(option)
#     if states <= cfg.brute_force_cap:
#         index_options = [[None, *[parents.index(parent) for parent in options]] for options in possible]
#         for option in product(*index_options):
#             consider(option)
#     else:
#         option: list[int | None] = [None] * len(children); local = dict(used)
#         for index in sorted(range(len(children)), key=lambda pos: children[pos]["id"]):
#             amount = _money(children[index])
#             for parent in sorted(possible[index], key=lambda item: item["id"]):
#                 budget = _available(parent, local)
#                 if amount is not None and budget is not None and amount <= budget + cfg.money_tolerance:
#                     option[index] = parents.index(parent); local[parent["id"]] = local.get(parent["id"], Decimal("0")) + amount; break
#         consider(tuple(option))
#     if not best_options:
#         return []
#     ambiguous = len({tuple(item) for item in best_options}) > 1
#     chosen = best_options[0]
#     result = []
#     for index, parent_index in enumerate(chosen):
#         if parent_index is not None:
#             parent, child = parents[parent_index], children[index]
#             result.append((parent, child, ambiguous))
#     return result


# def _link_layers(parents: list[dict[str, Any]], children: list[dict[str, Any]], connections: list[dict[str, Any]], parent_of: dict[str, list[str]], children_of: dict[str, list[str]], used: dict[str, Decimal], rejected: list[dict[str, Any]], cfg: FlowConfig) -> None:
#     # 1. Exact IDs, then similar IDs only with independent corroboration.
#     for child in children:
#         candidates = []
#         for parent in parents:
#             rule, score = _id_match(parent, child, cfg)
#             if not rule:
#                 continue
#             bank_ok = _bank_match(parent, child, cfg)[0]
#             amount_ok = _same_money(_money(parent), _money(child), cfg)
#             if rule == "transaction_id_similar" and not (bank_ok or amount_ok):
#                 rejected.append({"from": parent["id"], "to": child["id"], "reason": "similar_id_without_bank_or_amount_agreement"}); continue
#             if _can_assign(parent, child, used, cfg):
#                 candidates.append((score, parent, rule))
#         if candidates:
#             score, parent, rule = max(candidates, key=lambda item: (item[0], item[1]["id"]))
#             _add_connection(connections, parent, child, rule, cfg.confidence_id_exact if rule.endswith("exact") else cfg.confidence_id_similar, used, parent_of, children_of, len(candidates) > 1)
#     # 2. Exact bank + disputed (fallback transaction) amount.
#     for child in children:
#         if parent_of[child["id"]]:
#             continue
#         candidates = [parent for parent in parents if _bank_match(parent, child, cfg)[0] and _same_money(_money(parent), _money(child), cfg) and _can_assign(parent, child, used, cfg)]
#         if candidates:
#             parent = sorted(candidates, key=lambda item: item["id"])[0]
#             _add_connection(connections, parent, child, "bank_and_amount", cfg.confidence_bank_amount, used, parent_of, children_of, len(candidates) > 1)
#     # 3. Remaining bank-group assignment (single partial child allowed).
#     remaining = [child for child in children if not parent_of[child["id"]]]
#     for parent, child, ambiguous in _split_assign(parents, remaining, used, cfg):
#         amount = _money(child); budget = _available(parent, used)
#         exact = budget is not None and amount is not None and _same_money(budget, amount, cfg)
#         _add_connection(connections, parent, child, "bank_split_amount", cfg.confidence_split_exact if exact else cfg.confidence_split_partial, used, parent_of, children_of, ambiguous)
#     # 4. Merge only children still parentless; merged edges are explicit.
#     for child in children:
#         if parent_of[child["id"]] or _money(child) is None:
#             continue
#         options = [parent for parent in parents if _bank_match(parent, child, cfg)[0] and (_available(parent, used) or Decimal("0")) > 0]
#         groups = [group for size in range(2, min(cfg.max_subset_size, len(options)) + 1) for group in combinations(options, size) if _same_money(sum((_available(parent, used) or Decimal("0") for parent in group), Decimal("0")), _money(child), cfg)]
#         if groups:
#             group = sorted(groups, key=lambda value: tuple(parent["id"] for parent in value))[0]
#             for parent in group:
#                 # A merge records each parent's own remaining contribution;
#                 # charging the full child amount to every parent would break
#                 # the budget invariant.
#                 contribution = _available(parent, used)
#                 _add_connection(connections, parent, child, "multiple_parents_merge", cfg.confidence_merge, used, parent_of, children_of, len(groups) > 1, True, contribution)


# def _attach_holds(nodes: list[dict[str, Any]], holds: list[dict[str, Any]], cfg: FlowConfig) -> tuple[list[dict[str, Any]], set[str]]:
#     links, matched = [], set()
#     for hold in holds:
#         account, amount = _normal_account(_value(hold["row"], "account")), _decimal(_value(hold["row"], "hold_amount"))
#         primary, embedded = _id_values(hold, cfg)
#         candidates = [node for node in nodes if account and account == _normal_account(_value(node["row"], "account"))]
#         method, confidence = "account", cfg.confidence_hold_account
#         if not candidates and (primary or embedded):
#             sought = embedded | ({primary} if primary else set())
#             candidates = [node for node in nodes if sought & (_id_values(node, cfg)[1] | ({_id_values(node, cfg)[0]} if _id_values(node, cfg)[0] else set()))]
#             method, confidence = "utr_in_hold_remarks", cfg.confidence_hold_utr
#         if not candidates and amount is not None:
#             candidates = [node for node in nodes if _bank_match(node, hold, cfg)[0] and (_actual_money(node) or Decimal("-1")) + cfg.money_tolerance >= amount]
#             method, confidence = "bank_and_partial_hold_amount", cfg.confidence_hold_bank_amount
#         if not candidates:
#             continue
#         def rank(node: dict[str, Any]) -> tuple[int, Decimal, int, str]:
#             bank = 0 if _bank_match(node, hold, cfg)[0] else 1
#             gap = abs((_actual_money(node) or Decimal("0")) - (amount or Decimal("0")))
#             ids = _id_values(node, cfg)[1] | ({_id_values(node, cfg)[0]} if _id_values(node, cfg)[0] else set())
#             return bank, gap, 0 if ids & (embedded | ({primary} if primary else set())) else 1, node["id"]
#         node = min(candidates, key=rank)
#         link = {"hold": _ref(hold), "node": _ref(node), "matchRule": method, "confidence": confidence, "amount": str(amount) if amount is not None else None}
#         links.append(link); matched.add(hold["id"]); node["frozenAmount"] += amount or Decimal("0"); node["holdLinks"].append(link)
#     return links, matched


# def _reported_totals(payloads: list[tuple[str, dict[str, Any]]]) -> tuple[Decimal | None, Decimal | None]:
#     fraud = lien = None
#     for _table, payload in payloads:
#         for row in payload.get("rows", []):
#             if not isinstance(row, dict): continue
#             description = str(_value(row, "description") or "").lower()
#             value = _decimal(_value(row, "transaction_amount"))
#             if value is None: continue
#             if fraud is None and any(word in description for word in ("fraud", "disputed", "complaint")): fraud = value
#             if lien is None and any(word in description for word in ("lien", "hold", "frozen")): lien = value
#     return fraud, lien


# def build_transaction_flow(output_dir: str | Path, progress: Callable[[str], None] | None = None, config: FlowConfig | None = None) -> dict[str, Any]:
#     """Build ``transaction_flow.json`` and ``no_transaction_records.json``."""
#     cfg, directory = config or FlowConfig(), Path(output_dir)
#     payloads = _payloads(directory)
#     hold_payloads = [(table, payload) for table, payload in payloads if _is_hold(table, payload)]
#     trail_payloads = [(table, payload) for table, payload in payloads if _is_trail(table, payload)]
#     base_payloads = [(table, payload) for table, payload in payloads if _is_base(table, payload)]
#     base_nodes = [node for table, payload in base_payloads for node in _make_nodes(table, payload["rows"], 0, cfg)]
#     raw_trail = []
#     for table, payload in trail_payloads:
#         for row in payload["rows"]:
#             try: layer = int(_value(row, "layer"))
#             except (TypeError, ValueError): continue
#             if layer > 0: raw_trail.extend(_make_nodes(table, [row], layer, cfg))
#     by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
#     for node in raw_trail:
#         primary, _ = _id_values(node, cfg)
#         key = (node["layer"], _value(node["row"], "bank"), _value(node["row"], "account"), primary, _money(node))
#         if key in by_key: by_key[key]["sourceRowIds"].extend(node["sourceRowIds"])
#         else: by_key[key] = node
#     trail_nodes = sorted(by_key.values(), key=lambda node: node["id"])
#     hold_nodes = [node for table, payload in hold_payloads for node in _make_nodes(table, payload["rows"], -1, cfg)]
#     nodes = [*base_nodes, *trail_nodes]
#     for node in nodes: node["frozenAmount"], node["holdLinks"] = Decimal("0"), []
#     grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
#     for node in nodes: grouped[node["layer"]].append(node)
#     trail_layers = sorted(layer for layer in grouped if layer > 0)
#     layers = ([0] if base_nodes else []) + trail_layers
#     connections: list[dict[str, Any]] = []; parent_of: dict[str, list[str]] = defaultdict(list); children_of: dict[str, list[str]] = defaultdict(list); used: dict[str, Decimal] = defaultdict(lambda: Decimal("0")); rejected = []
#     for previous, current in zip(layers, layers[1:]):
#         if progress: progress(f"Matching transaction layer {previous} to layer {current}.")
#         _link_layers(grouped[previous], grouped[current], connections, parent_of, children_of, used, rejected, cfg)
#     hold_links, attached_holds = _attach_holds(nodes, hold_nodes, cfg)
#     no_flow = [_ref(node) for node in nodes if not parent_of[node["id"]] and not children_of[node["id"]] and not node["holdLinks"]]
#     unlinked = [_ref(node) for node in trail_nodes if not parent_of[node["id"]]]
#     untraced = [_ref(node) for node in base_nodes if not children_of[node["id"]]]
#     base_total = sum((_money(node) or Decimal("0") for node in base_nodes), Decimal("0")); hold_total = sum((_decimal(_value(hold["row"], "hold_amount")) or Decimal("0") for hold in hold_nodes), Decimal("0")); fraud_total, lien_total = _reported_totals(payloads)
#     primary_lengths = [len(primary) for node in nodes if (primary := _id_values(node, cfg)[0])]
#     typical = Counter(primary_lengths).most_common(1)[0][0] if primary_lengths else None
#     outliers = [node["id"] for node in nodes if typical is not None and cfg.id_length_window is not None and (primary := _id_values(node, cfg)[0]) and abs(len(primary) - typical) > cfg.id_length_window]
#     checks = {"missingFieldAliases": sorted(alias for alias, names in FIELD_ALIASES.items() if not any(set(names) & _fields(payload) for _table, payload in payloads)), "baseDebitTotal": str(base_total), "reportedFraudTotal": str(fraud_total) if fraud_total is not None else None, "baseDebitsMatchReportedFraud": None if fraud_total is None else _same_money(base_total, fraud_total, cfg), "holdTotal": str(hold_total), "reportedLienTotal": str(lien_total) if lien_total is not None else None, "holdsMatchReportedLien": None if lien_total is None else _same_money(hold_total, lien_total, cfg), "childrenWithMultipleParents": [child for child, parents in parent_of.items() if len(parents) > 1 and not all(item["merged"] for item in connections if item["to"][0]["id"] == child)], "parentBudgetViolations": [node["id"] for node in nodes if (budget := _available(node, used)) is not None and budget < -cfg.money_tolerance], "typicalIdLength": typical, "idLengthOutliers": outliers}
#     nodes_output = []
#     for node in nodes:
#         row = _ref(node); row.update({"frozenAmount": str(node["frozenAmount"]), "unaccountedAmount": str(_available(node, used)) if _available(node, used) is not None else None, "holdLinks": node["holdLinks"]}); nodes_output.append(row)
#     flow = {"layers": layers, "connectionCount": len(connections), "connections": connections, "nodes": nodes_output, "endOfTrail": [_ref(node) for node in trail_nodes if parent_of[node["id"]] and not children_of[node["id"]]], "rejectedCandidates": rejected, "checks": checks, "holdLinks": hold_links, "unmatchedCount": len(no_flow)}
#     no_records = {"noFlowRecords": no_flow, "unlinkedRows": unlinked, "untracedBase": untraced, "unmatchedHolds": [_ref(hold) for hold in hold_nodes if hold["id"] not in attached_holds]}
#     flow_path, no_path = directory / "transaction_flow.json", directory / "no_transaction_records.json"
#     flow_path.write_text(json.dumps(flow, ensure_ascii=False, indent=2), encoding="utf-8"); no_path.write_text(json.dumps(no_records, ensure_ascii=False, indent=2), encoding="utf-8")
#     if progress: progress(f"Built {len(connections)} flow connection(s); {len(no_flow)} record(s) have no flow.")
#     return {"flowPath": str(flow_path), "noFlowPath": str(no_path), "connectionCount": len(connections), "unmatchedCount": len(no_flow)}



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