"""
Boundary test for LOW_CONFIDENCE_LINK

Tests that the real build_graph_payload / LOW_CONFIDENCE constant
in services/caseGraph/graph.py behaves as the paper describes:

  c <  0.75  -> LOW_CONFIDENCE_LINK raised     (flagged)
  c == 0.75  -> NOT flagged   (strict less-than, so 0.75 itself is safe)
  c >  0.75  -> NOT flagged

Run from the backend/ directory:
    python -m pytest evidence/confidence/test_boundary.py -v
or from the repo root:
    cd backend && python -m pytest ../evidence/confidence/test_boundary.py -v
"""
import sys
import pathlib

# Allow import from the backend package when run from the repo root.
_HERE = pathlib.Path(__file__).resolve()
_BACKEND = _HERE.parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from decimal import Decimal
from services.caseGraph.graph import build_graph_payload, LOW_CONFIDENCE


def _node(node_id, layer, amount=100):
    return {
        "node_id": node_id,
        "layer": layer,
        "bank": "Test Bank",
        "disputed_amount": Decimal(str(amount)),
    }


def _edge(source, target, confidence, ambiguous=False):
    return {
        "from_node": source,
        "to_node": target,
        "amount_passed": Decimal("100"),
        "confidence": Decimal(str(confidence)),
        "ambiguous": 1 if ambiguous else 0,
        "merged": 0,
        "amount_estimated": 0,
        "match_rule": "test_rule",
    }


def _has_low_confidence_flag(graph, node_id):
    for node in graph["nodes"]:
        if node["id"] == node_id:
            codes = [r["code"] for r in node["completeness"]["reasons"]]
            return "LOW_CONFIDENCE_LINK" in codes
    raise KeyError(f"Node {node_id!r} not found in graph")


def _build(confidence_value):
    nodes = [_node("victim", 0), _node("child", 1)]
    edges = [_edge("victim", "child", confidence_value)]
    return build_graph_payload({"ack_no": "TEST"}, nodes, edges, [], [])


def test_threshold_constant_is_0_75():
    assert LOW_CONFIDENCE == 0.75, f"LOW_CONFIDENCE is {LOW_CONFIDENCE!r}, expected 0.75"


def test_exact_threshold_not_flagged():
    """c == 0.75 must NOT raise LOW_CONFIDENCE_LINK (strict less-than)."""
    assert not _has_low_confidence_flag(_build(0.75), "child"), \
        "FAIL: c = 0.75 was flagged. Comparison must be strictly <, not <=."


def test_just_below_threshold_flagged():
    """c = 0.7499 must raise LOW_CONFIDENCE_LINK."""
    assert _has_low_confidence_flag(_build(0.7499), "child"), \
        "FAIL: c = 0.7499 was NOT flagged."


def test_just_above_threshold_not_flagged():
    """c = 0.7501 must NOT raise LOW_CONFIDENCE_LINK."""
    assert not _has_low_confidence_flag(_build(0.7501), "child"), \
        "FAIL: c = 0.7501 was flagged."


def test_ambiguous_alone_triggers_flag():
    """ambiguous=True with c=1.0 should still trigger the flag."""
    nodes = [_node("victim", 0), _node("child", 1)]
    edges = [_edge("victim", "child", 1.0, ambiguous=True)]
    graph = build_graph_payload({"ack_no": "TEST"}, nodes, edges, [], [])
    assert _has_low_confidence_flag(graph, "child"), \
        "FAIL: ambiguous=True with c=1.0 did not raise LOW_CONFIDENCE_LINK."


def test_none_confidence_does_not_flag():
    """confidence=None must NOT trigger the flag (the None-guard in the condition)."""
    nodes = [_node("victim", 0), _node("child", 1)]
    edge_row = {
        "from_node": "victim", "to_node": "child",
        "amount_passed": Decimal("100"), "confidence": None,
        "ambiguous": 0, "merged": 0, "amount_estimated": 0, "match_rule": "test_rule",
    }
    graph = build_graph_payload({"ack_no": "TEST"}, nodes, [edge_row], [], [])
    assert not _has_low_confidence_flag(graph, "child"), \
        "FAIL: confidence=None incorrectly raised LOW_CONFIDENCE_LINK."


if __name__ == "__main__":
    tests = [
        test_threshold_constant_is_0_75,
        test_exact_threshold_not_flagged,
        test_just_below_threshold_flagged,
        test_just_above_threshold_not_flagged,
        test_ambiguous_alone_triggers_flag,
        test_none_confidence_does_not_flag,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed.")
    sys.exit(0 if failed == 0 else 1)
