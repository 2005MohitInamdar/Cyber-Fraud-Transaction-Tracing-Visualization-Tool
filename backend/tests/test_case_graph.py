from datetime import date
from decimal import Decimal

from services.caseGraph.graph import build_graph_payload


def test_build_graph_payload_normalises_rows_and_drops_dangling_edges() -> None:
    graph = build_graph_payload(
        {
            "ack_no": "ACK-1",
            "base_debit_total": Decimal("100.00"),
            "holds_match_lien": 1,
            "unmatched_hold_count": 1,
        },
        [
            {"node_id": "node:b", "layer": 1, "bank": "Bank B", "amount_estimated": 1, "embedded_ids": '["x"]'},
            {"node_id": "node:a", "layer": 0, "bank": "Bank A", "tx_amount": Decimal("100"), "root_ids": b'["root"]'},
        ],
        [
            {"from_node": "node:a", "to_node": "node:b", "match_rule": "utr", "confidence": Decimal("0.9")},
            {"from_node": "node:a", "to_node": "missing", "match_rule": "utr", "confidence": Decimal("0.9")},
        ],
        [{"hold_id": "hold:1", "hold_amount": Decimal("50"), "hold_date": date(2026, 1, 1), "action_taken_by": "Bank B"}],
        [{"hold_id": "hold:1", "node_id": "node:b", "match_rule": "account", "confidence": Decimal("0.8"), "amount": Decimal("50")}],
    )

    assert graph["hasGraph"] is True
    assert graph["summary"]["nodeCount"] == 2
    assert graph["summary"]["edgeCount"] == 1
    assert graph["summary"]["holdsMatchLien"] is True
    assert graph["nodes"][0]["id"] == "node:a"
    assert graph["nodes"][0]["role"] == "victim"
    assert graph["nodes"][1]["role"] == "endOfTrail"
    assert graph["nodes"][1]["holds"][0]["date"] == "2026-01-01"


def test_build_graph_payload_without_case_has_no_graph() -> None:
    assert build_graph_payload(None, [], [], [], []) == {
        "hasGraph": False,
        "summary": {
            "ackNo": None, "status": None, "baseDebitTotal": None,
            "reportedFraudTotal": None, "holdTotal": None, "reportedLienTotal": None,
            "holdsMatchLien": False, "nodeCount": 0, "edgeCount": 0,
            "layers": [], "unmatchedHoldCount": 0,
        },
        "nodes": [], "edges": [],
    }
