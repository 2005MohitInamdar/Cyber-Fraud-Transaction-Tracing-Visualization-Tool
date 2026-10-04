from decimal import Decimal

from services.caseGraph.graph import build_graph_payload


def _node(node_id, layer, amount=100, **extra):
    return {"node_id": node_id, "layer": layer, "bank": extra.pop("bank", "Example Bank"), "disputed_amount": Decimal(str(amount)), **extra}


def _edge(source, target, amount=100, **extra):
    return {"from_node": source, "to_node": target, "amount_passed": Decimal(str(amount)), "confidence": Decimal(".9"), **extra}


def _graph(nodes, edges, holds=None, links=None, pending=None):
    return build_graph_payload({"ack_no": "ACK"}, nodes, edges, holds or [], links or [], pending)


def _item(graph, node_id):
    return next(item for item in graph["incomplete"] if item["nodeId"] == node_id)


def _codes(graph, node_id):
    return [reason["code"] for reason in _item(graph, node_id)["reasons"]]


def test_chain_only_flags_dead_end() -> None:
    graph = _graph([_node("a", 0), _node("b", 1), _node("c", 2)], [_edge("a", "b"), _edge("b", "c")])
    assert graph["nodes"][0]["completeness"]["status"] == "complete"
    assert graph["nodes"][1]["completeness"]["status"] == "complete"
    assert _codes(graph, "c") == ["END_NO_STATUS"]


def test_full_linked_hold_resolves_dead_end_and_partial_hold_does_not() -> None:
    nodes = [_node("a", 0), _node("c", 1, Decimal("1998.27"))]
    edges = [_edge("a", "c", Decimal("1998.27"))]
    full = _graph(nodes, edges, [{"hold_id": "h", "hold_amount": Decimal("1998.27")}], [{"hold_id": "h", "node_id": "c", "amount": Decimal("1998.27")}])
    assert full["nodes"][1]["completeness"] == {"status": "complete", "reasons": [], "resolvedBy": "hold"}
    partial = _graph(nodes, edges, [{"hold_id": "h", "hold_amount": Decimal("21.42")}], [{"hold_id": "h", "node_id": "c", "amount": Decimal("21.42")}])
    assert _codes(partial, "c") == ["UNACCOUNTED_AMOUNT"]


def test_pending_uses_exact_bank_token_sets_and_withdrawal_notes_resolve() -> None:
    nodes = [_node("a", 0), _node("c", 1, bank="NSDL Payments Bank Ltd")]
    edges = [_edge("a", "c")]
    short = _graph(nodes, edges, pending=[{"bank": "NSDL"}])
    assert "END_NO_STATUS" in _codes(short, "c")
    exact = _graph(nodes, edges, pending=[{"bank": "NSDL Payments Bank Ltd"}])
    assert exact["nodes"][1]["completeness"]["resolvedBy"] == "pending"
    withdrawal = _graph([_node("a", 0), _node("c", 1, remarks="Cash withdrawn at ATM")], edges)
    assert withdrawal["nodes"][1]["completeness"]["resolvedBy"] == "withdrawal_note"
    balance = _graph([_node("a", 0), _node("c", 1, remarks="Statement Ending Balance =276.50")], edges)
    assert "END_NO_STATUS" in _codes(balance, "c")


def test_unmatched_links_amounts_orphans_and_deterministic_order() -> None:
    graph = _graph(
        [_node("victim", 0), _node("orphan", 1), _node("parent", 1, Decimal("29800.15")), _node("child", 2, Decimal("26881.84"))],
        [_edge("parent", "child", Decimal("26881.84"))],
        [{"hold_id": "linked", "hold_amount": Decimal("2818.31")}, {"hold_id": "orphan-hold", "hold_amount": 20}],
        [{"hold_id": "linked", "node_id": "parent", "amount": Decimal("2818.31")}],
    )
    assert "VICTIM_UNTRACED" in _codes(graph, "victim")
    assert "NO_INCOMING_LINK" in _codes(graph, "orphan")
    assert "UNACCOUNTED_AMOUNT" in _codes(graph, "parent")
    assert graph["orphanHolds"][0]["holdId"] == "orphan-hold"
    assert graph["summary"]["orphanHoldCount"] == 1
    assert graph["incomplete"] == sorted(graph["incomplete"], key=lambda item: ({"high": 0, "medium": 1, "low": 2}[item["severity"]], item["layer"], item["nodeId"]))


def test_old_callers_without_pending_rows_remain_supported() -> None:
    graph = build_graph_payload({"ack_no": "ACK"}, [], [], [], [])
    assert graph["hasGraph"] is True
    assert graph["incomplete"] == []
