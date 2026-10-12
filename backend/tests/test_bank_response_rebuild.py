import json
from decimal import Decimal
from pathlib import Path

import pytest

from services.bankResponse import rebuild
from services.bankResponse.rebuild import export_original_rows, response_rows_for_build
from services.caseGraph.graph import build_graph_payload
from services.pdf_extraction.transaction_flow import build_transaction_flow


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _query, _params):
        return None

    def fetchall(self):
        return self.rows


class _ExportCursor:
    def __init__(self, payload_rows=None):
        self.payload_rows = payload_rows or []
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params):
        if "FROM case_source_payloads" in query:
            self.current = self.payload_rows
        elif "information_schema.tables" in query:
            self.current = []
        else:
            self.current = []

    def fetchall(self):
        return self.current

    def fetchone(self):
        return self.current[0] if self.current else None


class _ExportConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self, **_kwargs):
        return self._cursor


def _mapped_payload(table, rows):
    return {"table": table, "columns": list(rows[0]) if rows else [], "rows": rows}


def test_response_layer_shift_and_anchor_only_on_first_layer():
    originals = {
        "lien_transactions": {"table": "lien_transactions", "columns": ["s_no", "layer"], "rows": [{"s_no": 9, "layer": 4}]},
        "hold_accounts": {"table": "hold_accounts", "columns": ["s_no"], "rows": []},
    }
    cursor = _Cursor([
        {"table_name": "lien_transactions", "data": {"s_no": 1, "layer": 1, "transaction_id": "A"}},
        {"table_name": "lien_transactions", "data": {"s_no": 2, "layer": 2, "transaction_id": "B"}},
        {"table_name": "hold_accounts", "data": {"s_no": 1, "hold_amount": "10.00"}},
    ])

    response_rows_for_build(cursor, {"id": 5, "layer_shift": 3, "anchor_node_id": "node:root"}, originals)

    lien = originals["lien_transactions"]["rows"]
    assert lien[1]["layer"] == 4
    assert lien[1]["explicit_parent"] == {"nodeId": "node:root"}
    assert lien[2]["layer"] == 5
    assert "explicit_parent" not in lien[2]
    assert [row["s_no"] for row in lien] == [9, 10, 11]
    assert originals["hold_accounts"]["rows"][0]["s_no"] == 1


def test_no_onward_annotation_resolves_dead_end_only_after_annotation_revision():
    nodes = [
        {"node_id": "victim", "layer": 0, "disputed_amount": Decimal("100.00")},
        {"node_id": "bank", "layer": 1, "disputed_amount": Decimal("100.00")},
    ]
    edges = [{"from_node": "victim", "to_node": "bank", "amount_passed": Decimal("100.00"), "confidence": Decimal("1.0")}]
    case = {"ack_no": "ACK", "revision": 2}
    before = build_graph_payload(case, nodes, edges, [], [])
    after = build_graph_payload(case, nodes, edges, [], [], annotations=[{"node_id": "bank", "kind": "no_onward"}])
    assert any(item["nodeId"] == "bank" for item in before["incomplete"])
    assert not any(item["nodeId"] == "bank" for item in after["incomplete"])
    assert after["nodes"][1]["completeness"]["resolvedBy"] == "no_onward"


def test_source_payload_export_round_trips_through_flow_builder(monkeypatch, tmp_path: Path):
    payloads = {
        "complaint_transactions": _mapped_payload("complaint_transactions", [
            {"transaction_id": "ROOTUTR123456", "transaction_amount": "100", "bank_fi": "Victim Bank"},
        ]),
        "lien_transactions": _mapped_payload("lien_transactions", [
            {"layer": 1, "bank_fi": "Airtel Payments Bank", "account_no": "acct-1", "transaction_id": "ROOTUTR123456", "transaction_amount": "100", "disputed_amount": "100", "action_taken_by": "Victim Bank"},
            {"layer": 2, "bank_fi": "Equitas Small Finance Bank", "account_no": "acct-2", "transaction_id": "NEXTUTR123456", "transaction_amount": "100", "disputed_amount": "100", "action_taken_by": "Airtel Payments Bank"},
        ]),
        "hold_accounts": _mapped_payload("hold_accounts", []),
    }
    source_rows = [{"table_name": table, "payload": json.dumps(payload)} for table, payload in payloads.items()]
    monkeypatch.setattr(rebuild, "get_connection", lambda: _ExportConnection(_ExportCursor(source_rows)))
    exported = export_original_rows("user", "011f0a39-879c-463e-b459-b3f46e44b656")

    for table, payload in payloads.items():
        (tmp_path / f"{table}.json").write_text(json.dumps(payload), encoding="utf-8")
    original = json.loads(Path(build_transaction_flow(tmp_path)["flowPath"]).read_text(encoding="utf-8"))

    rebuilt_dir = tmp_path / "round-trip"
    rebuilt_dir.mkdir()
    for table, payload in exported.items():
        (rebuilt_dir / f"{table}.json").write_text(json.dumps(payload), encoding="utf-8")
    round_trip = json.loads(Path(build_transaction_flow(rebuilt_dir)["flowPath"]).read_text(encoding="utf-8"))
    assert round_trip["nodes"].keys() == original["nodes"].keys()
    assert round_trip["connections"] == original["connections"]
    assert round_trip["checks"] == original["checks"]


def test_source_export_falls_back_to_safe_extracted_json_and_empty_export_aborts(monkeypatch, tmp_path: Path):
    upload_id = "011f0a39-879c-463e-b459-b3f46e44b656"
    extracted = tmp_path / upload_id
    extracted.mkdir()
    payload = _mapped_payload("complaint_transactions", [{"transaction_id": "ROOTUTR123456", "transaction_amount": "100"}])
    (extracted / "complaint_transactions.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(rebuild, "get_connection", lambda: _ExportConnection(_ExportCursor()))
    monkeypatch.setattr(rebuild, "_EXTRACTED_DIR", tmp_path)
    assert export_original_rows("user", upload_id)["complaint_transactions"] == payload

    (extracted / "complaint_transactions.json").unlink()
    with pytest.raises(ValueError, match="original lien_transactions or complaint_transactions"):
        export_original_rows("user", upload_id)


def test_original_plus_response_keeps_original_ids_and_adds_nodes(tmp_path: Path):
    originals = {
        "complaint_transactions": _mapped_payload("complaint_transactions", [
            {"transaction_id": "ROOTUTR123456", "transaction_amount": "100", "bank_fi": "Victim Bank"},
        ]),
        "lien_transactions": _mapped_payload("lien_transactions", [
            {"s_no": 1, "layer": 1, "bank_fi": "Airtel Payments Bank", "account_no": "acct-1", "transaction_id": "ROOTUTR123456", "transaction_amount": "100", "disputed_amount": "100", "action_taken_by": "Victim Bank"},
        ]),
        "hold_accounts": _mapped_payload("hold_accounts", []),
    }
    for table, payload in originals.items():
        (tmp_path / f"{table}.json").write_text(json.dumps(payload), encoding="utf-8")
    original_flow = json.loads(Path(build_transaction_flow(tmp_path)["flowPath"]).read_text(encoding="utf-8"))

    response_cursor = _Cursor([
        {"table_name": "lien_transactions", "data": {"s_no": 1, "layer": 2, "bank_fi": "Equitas Small Finance Bank", "account_no": "acct-2", "transaction_id": "NEXTUTR123456", "transaction_amount": "100", "disputed_amount": "100", "action_taken_by": "Airtel Payments Bank"}},
    ])
    response_rows_for_build(response_cursor, {"id": 9, "layer_shift": 0}, originals)
    rebuilt_dir = tmp_path / "with-response"
    rebuilt_dir.mkdir()
    for table, payload in originals.items():
        (rebuilt_dir / f"{table}.json").write_text(json.dumps(payload), encoding="utf-8")
    rebuilt_flow = json.loads(Path(build_transaction_flow(rebuilt_dir)["flowPath"]).read_text(encoding="utf-8"))

    old_ids = set(original_flow["nodes"])
    assert old_ids <= set(rebuilt_flow["nodes"])
    assert len(rebuilt_flow["nodes"]) > len(original_flow["nodes"])
    old_edges = {(edge["from"], target) for edge in original_flow["connections"] for target in edge["to"]}
    new_edges = {(edge["from"], target) for edge in rebuilt_flow["connections"] for target in edge["to"]}
    assert old_edges <= new_edges


def test_unanchored_19_lien_6_hold_response_joins_existing_flow(tmp_path: Path):
    originals = {
        "complaint_transactions": _mapped_payload("complaint_transactions", [
            {"transaction_id": "ROOTUTR123456", "transaction_amount": "100", "bank_fi": "Victim Bank"},
            {"transaction_id": "INDIANUTR123456", "transaction_amount": "500", "bank_fi": "Indian Bank"},
        ]),
        "lien_transactions": _mapped_payload("lien_transactions", [
            {"s_no": 1, "layer": 1, "bank_fi": "Airtel Payments Bank", "account_no": "airtel-1", "transaction_id": "ROOTUTR123456", "transaction_amount": "100", "disputed_amount": "100", "action_taken_by": "Victim Bank"},
        ]),
        "hold_accounts": _mapped_payload("hold_accounts", []),
    }
    for table, payload in originals.items():
        (tmp_path / f"{table}.json").write_text(json.dumps(payload), encoding="utf-8")
    before = json.loads(Path(build_transaction_flow(tmp_path)["flowPath"]).read_text(encoding="utf-8"))

    response_rows = [
        {"table_name": "lien_transactions", "data": {"s_no": 1, "layer": 2, "bank_fi": "Equitas Small Finance Bank", "account_no": "equitas-1", "transaction_id": "EQUITASUTR123456", "transaction_amount": "100", "disputed_amount": "100", "action_taken_by": "Airtel Payments Bank"}},
        {"table_name": "lien_transactions", "data": {"s_no": 2, "layer": 1, "bank_fi": "Canara Bank", "account_no": "canara-1", "transaction_id": "CANARAUTR123456", "transaction_amount": "500", "disputed_amount": "500", "action_taken_by": "Indian Bank"}},
    ]
    response_rows.extend(
        {"table_name": "lien_transactions", "data": {"s_no": index + 3, "layer": 4, "bank_fi": f"Other Bank {index}", "account_no": f"other-{index}", "transaction_id": f"OTHERUTR{index:06d}", "transaction_amount": "1", "disputed_amount": "1", "action_taken_by": "Unknown Bank"}}
        for index in range(17)
    )
    response_rows.extend(
        {"table_name": "hold_accounts", "data": {"s_no": index + 1, "account_no": f"hold-{index}", "hold_amount": "10", "action_taken_by": "Bank"}}
        for index in range(6)
    )
    response_rows_for_build(_Cursor(response_rows), {"id": 10, "layer_shift": 0, "anchor_node_id": None}, originals)
    after_dir = tmp_path / "sample-response"
    after_dir.mkdir()
    for table, payload in originals.items():
        (after_dir / f"{table}.json").write_text(json.dumps(payload), encoding="utf-8")
    after = json.loads(Path(build_transaction_flow(after_dir)["flowPath"]).read_text(encoding="utf-8"))

    assert len(response_rows) == 25  # 19 lien rows and 6 holds, as in the supplied sample shape.
    assert set(before["nodes"]) <= set(after["nodes"])
    labels = {(node.get("bank"), node.get("actionTakenBy")) for node in after["nodes"].values()}
    assert ("Equitas Small Finance Bank", "Airtel Payments Bank") in labels
    assert ("Canara Bank", "Indian Bank") in labels
    links = {(edge["from"], target) for edge in after["connections"] for target in edge["to"]}
    node_by_bank = {node["bank"]: node_id for node_id, node in after["nodes"].items()}
    assert (node_by_bank["Airtel Payments Bank"], node_by_bank["Equitas Small Finance Bank"]) in links
    assert (node_by_bank["Indian Bank"], node_by_bank["Canara Bank"]) in links
