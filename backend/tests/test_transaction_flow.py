"""Invariant tests for data-driven transaction-flow reconstruction."""

import json
import random
from pathlib import Path

from services.pdf_extraction.transaction_flow import FlowConfig, build_transaction_flow


def _write(directory: Path, table: str, rows: list[dict]) -> None:
    (directory / f"{table}.json").write_text(
        json.dumps({"table": table, "rows": rows}), encoding="utf-8"
    )


def _trail(seed: int) -> tuple[list[dict], list[dict], list[dict]]:
    randomizer = random.Random(seed)
    total = randomizer.randrange(1000, 9000)
    first = total // 2
    second = total - first
    identifier = "".join(str(randomizer.randrange(10)) for _ in range(10))
    bank = " ".join(("entity", str(randomizer.randrange(100, 999))))
    base = [{"transaction_id": identifier, "transaction_amount": str(total), "bank_fi": bank}]
    layers = [
        {"layer": 1, "bank_fi": bank, "account_no": "000" + str(seed), "transaction_id": identifier, "transaction_amount": str(total), "disputed_amount": str(total), "action_taken_by": "origin entity"},
        {"layer": 2, "bank_fi": "destination entity", "account_no": "00" + str(seed + 1), "transaction_id": "next" + identifier, "transaction_amount": str(first), "disputed_amount": str(first), "action_taken_by": bank},
        {"layer": 2, "bank_fi": "destination entity", "account_no": "00" + str(seed + 2), "transaction_id": "later" + identifier, "transaction_amount": str(second), "disputed_amount": str(second), "action_taken_by": bank},
    ]
    holds = [{"account_no": str(seed + 1), "hold_amount": str(first // 2), "action_taken_by": "destination entity", "reference_remarks": f"Root_UTR:{'next' + identifier}"}]
    return base, layers, holds


def test_links_are_stable_and_respect_budgets(tmp_path: Path) -> None:
    base, layers, holds = _trail(7)
    _write(tmp_path, "base_input", base)
    _write(tmp_path, "trail_input", layers)
    _write(tmp_path, "status_input", holds)
    result = build_transaction_flow(tmp_path, config=FlowConfig(id_length_window=2))
    flow = json.loads(Path(result["flowPath"]).read_text(encoding="utf-8"))
    assert flow["checks"]["parentBudgetViolations"] == []
    assert flow["checks"]["childrenWithMultipleParents"] == []
    assert len(flow["holdLinks"]) == 1
    first_links = {(item["from"]["id"], item["to"][0]["id"], item["matchRule"]) for item in flow["connections"]}

    random.Random(4).shuffle(layers)
    _write(tmp_path, "trail_input", layers)
    second = json.loads(Path(build_transaction_flow(tmp_path)["flowPath"]).read_text(encoding="utf-8"))
    second_links = {(item["from"]["id"], item["to"][0]["id"], item["matchRule"]) for item in second["connections"]}
    assert first_links == second_links


def test_similar_id_without_corrobation_is_rejected(tmp_path: Path) -> None:
    identifier = "1234567890"
    _write(tmp_path, "base_input", [{"transaction_id": identifier, "transaction_amount": "100", "bank_fi": "source"}])
    _write(tmp_path, "trail_input", [
        {"layer": 1, "bank_fi": "first", "account_no": "100", "transaction_id": identifier, "transaction_amount": "100", "disputed_amount": "100", "action_taken_by": "source"},
        {"layer": 2, "bank_fi": "second", "account_no": "200", "transaction_id": "1234567899", "transaction_amount": "12", "disputed_amount": "12", "action_taken_by": "unrelated"},
    ])
    flow = json.loads(Path(build_transaction_flow(tmp_path)["flowPath"]).read_text(encoding="utf-8"))
    assert any(item["reason"] == "similar_id_without_bank_or_amount_agreement" for item in flow["rejectedCandidates"])


def test_unmatched_holds_are_not_no_flow_records(tmp_path: Path) -> None:
    base, layers, holds = _trail(11)
    holds[0]["account_no"] = "not-an-account"
    holds[0]["reference_remarks"] = ""
    holds[0]["action_taken_by"] = "unrelated-status-entity"
    _write(tmp_path, "base_input", base)
    _write(tmp_path, "trail_input", layers)
    _write(tmp_path, "status_input", holds)
    result = build_transaction_flow(tmp_path)
    missing = json.loads(Path(result["noFlowPath"]).read_text(encoding="utf-8"))
    assert len(missing["unmatchedHolds"]) == 1
    assert all(item["table"] != "status_input" for item in missing["noFlowRecords"])


def test_merge_uses_each_parent_budget_once(tmp_path: Path) -> None:
    left, right = 37, 63
    _write(tmp_path, "base_input", [])
    _write(tmp_path, "trail_input", [
        {"layer": 1, "bank_fi": "merge-source", "account_no": "a", "transaction_id": "parent-one-123", "transaction_amount": str(left), "disputed_amount": str(left), "action_taken_by": "origin"},
        {"layer": 1, "bank_fi": "merge-source", "account_no": "b", "transaction_id": "parent-two-456", "transaction_amount": str(right), "disputed_amount": str(right), "action_taken_by": "origin"},
        {"layer": 2, "bank_fi": "merged-destination", "account_no": "c", "transaction_id": "child-three-789", "transaction_amount": str(left + right), "disputed_amount": str(left + right), "action_taken_by": "merge-source"},
    ])
    flow = json.loads(Path(build_transaction_flow(tmp_path)["flowPath"]).read_text(encoding="utf-8"))
    merged = [item for item in flow["connections"] if item["matchRule"] == "multiple_parents_merge"]
    assert len(merged) == 2
    assert flow["checks"]["parentBudgetViolations"] == []
