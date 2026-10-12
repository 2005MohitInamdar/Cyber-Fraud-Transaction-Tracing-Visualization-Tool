"""
pr_curve.py  –  Precision / recall analysis for the confidence threshold.

Usage:
    python pr_curve.py [path/to/links_to_label.csv]

Reads links_to_label.csv (rows where correct_label is filled: 1 = correct
link, 0 = incorrect link).  For each theta in 0.50, 0.55, ..., 0.95:

  Part A – Link-correctness prediction
  "c >= theta" predicts "this link is correct"
    TP = correct links with c >= theta
    FP = incorrect links with c >= theta
    FN = correct links with c <  theta
    TN = incorrect links with c < theta

  Part B – LOW_CONFIDENCE_LINK flag prediction
  "c < theta" predicts "this link is incorrect" (mirrors what the flag does)
    TP = incorrect links with c <  theta
    FP = correct links with c <  theta
    FN = incorrect links with c >= theta
    TN = correct links with c >= theta

Output:
    Printed markdown tables + pr_table.csv saved next to this script.
"""
import csv
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_DEFAULT_CSV = _HERE / "links_to_label.csv"


def _read(csv_path: pathlib.Path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            label = row.get("correct_label", "").strip()
            if label not in ("0", "1"):
                continue
            try:
                c = float(row["c"])
            except (ValueError, KeyError):
                continue
            rows.append({"c": c, "label": int(label), "case_id": row.get("case_id", "")})
    return rows


def _metrics(tp, fp, fn, tn):
    precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    recall    = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
    f1        = (2 * precision * recall / (precision + recall)
                 if (precision + recall) > 0 else float("nan"))
    return precision, recall, f1


def _fmt(v):
    return f"{v:.3f}" if v == v else "n/a"  # nan check


def _run(csv_path: pathlib.Path):
    rows = _read(csv_path)
    n_labelled  = len(rows)
    n_cases     = len(set(r["case_id"] for r in rows))
    n_correct   = sum(1 for r in rows if r["label"] == 1)
    n_incorrect = sum(1 for r in rows if r["label"] == 0)
    c_values    = sorted(set(r["c"] for r in rows))

    print(f"\n## Summary")
    print(f"- Labelled links : {n_labelled}")
    print(f"- Cases          : {n_cases}")
    print(f"- Correct (1)    : {n_correct}")
    print(f"- Incorrect (0)  : {n_incorrect}")
    print(f"- Distinct c     : {c_values}")

    if n_labelled < 30:
        print("\n> WARNING: n < 30 – results are not statistically meaningful.")
    if n_cases < 2:
        print("\n> WARNING: Only one case present – results may not generalise.")
    if len(c_values) <= 5:
        print(f"\n> NOTE: c takes only {len(c_values)} distinct values "
              f"({c_values}) – the P/R curve will be a step function.")

    thetas = [round(0.50 + i * 0.05, 2) for i in range(10)]  # 0.50 .. 0.95

    # ── Part A: c >= theta  => predict "correct" ──────────────────────────
    header_a = "| theta | TP | FP | FN | TN | precision | recall | F1 | flagged(c<theta) |"
    sep_a    = "|-------|----|----|----|-----|-----------|--------|----|-----------------|"
    print(f"\n### Part A – Correct-link prediction (c >= theta => predicted correct)\n")
    print(header_a); print(sep_a)
    part_a_rows = []
    for theta in thetas:
        tp = sum(1 for r in rows if r["label"] == 1 and r["c"] >= theta)
        fp = sum(1 for r in rows if r["label"] == 0 and r["c"] >= theta)
        fn = sum(1 for r in rows if r["label"] == 1 and r["c"] <  theta)
        tn = sum(1 for r in rows if r["label"] == 0 and r["c"] <  theta)
        flagged = fn + tn
        prec, rec, f1 = _metrics(tp, fp, fn, tn)
        print(f"| {theta:.2f} | {tp} | {fp} | {fn} | {tn} | "
              f"{_fmt(prec)} | {_fmt(rec)} | {_fmt(f1)} | {flagged} |")
        part_a_rows.append(dict(
            part="A", theta=theta, TP=tp, FP=fp, FN=fn, TN=tn,
            precision=_fmt(prec), recall=_fmt(rec), F1=_fmt(f1), flagged=flagged
        ))

    # ── Part B: c < theta  => predict "incorrect" (mirrors LOW_CONFIDENCE_LINK)
    header_b = "| theta | TP | FP | FN | TN | precision | recall | F1 |"
    sep_b    = "|-------|----|----|----|-----|-----------|--------|----|"
    print(f"\n### Part B – Incorrect-link detection (c < theta => predicted incorrect)\n")
    print(header_b); print(sep_b)
    part_b_rows = []
    for theta in thetas:
        tp = sum(1 for r in rows if r["label"] == 0 and r["c"] <  theta)
        fp = sum(1 for r in rows if r["label"] == 1 and r["c"] <  theta)
        fn = sum(1 for r in rows if r["label"] == 0 and r["c"] >= theta)
        tn = sum(1 for r in rows if r["label"] == 1 and r["c"] >= theta)
        prec, rec, f1 = _metrics(tp, fp, fn, tn)
        print(f"| {theta:.2f} | {tp} | {fp} | {fn} | {tn} | "
              f"{_fmt(prec)} | {_fmt(rec)} | {_fmt(f1)} |")
        part_b_rows.append(dict(
            part="B", theta=theta, TP=tp, FP=fp, FN=fn, TN=tn,
            precision=_fmt(prec), recall=_fmt(rec), F1=_fmt(f1), flagged="n/a"
        ))

    # ── Save CSV ──────────────────────────────────────────────────────────
    out_csv = _HERE / "pr_table.csv"
    fieldnames = ["part", "theta", "TP", "FP", "FN", "TN",
                  "precision", "recall", "F1", "flagged"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(part_a_rows + part_b_rows)
    print(f"\nSaved: {out_csv}")


if __name__ == "__main__":
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_CSV
    if not path.exists():
        sys.exit(f"ERROR: {path} not found.")
    _run(path)
