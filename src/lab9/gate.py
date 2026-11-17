"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems: list[str] = []
    metric_ids = {m["id"] for m in spec.get("metrics", [])}
    for name, value in pr.get("metrics", {}).items():
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"metric {name} is not finite")

    def row_keys(report: dict) -> set[tuple[str, int]]:
        return {(str(r.get("item_id")), int(r.get("seed"))) for r in report.get("rows", [])}

    expected = int(pr.get("manifest", {}).get("items", 0)) * len(pr.get("manifest", {}).get("seeds", []))
    keys, base_keys = row_keys(pr), row_keys(base)
    if expected and len(pr.get("rows", [])) != expected:
        problems.append(f"row count {len(pr.get('rows', []))} != manifest {expected}")
    if len(keys) != len(pr.get("rows", [])):
        problems.append("duplicate item_id/seed row")
    if keys != base_keys:
        problems.append("item_id/seed set differs from baseline")
    if pr.get("evaluator_failures"):
        problems.append("evaluator_failures is non-empty")
    for row in pr.get("rows", []):
        for metric in metric_ids:
            if metric in row and row[metric] is None:
                problems.append(f"{row.get('item_id')}@{row.get('seed')}: {metric} is missing")
    base_slices = {(str(r.get("item_id")), int(r.get("seed"))): tuple(r.get("slices") or []) for r in base.get("rows", [])}
    for row in pr.get("rows", []):
        key = (str(row.get("item_id")), int(row.get("seed")))
        if key in base_slices and tuple(row.get("slices") or []) != base_slices[key]:
            problems.append(f"{key[0]}@{key[1]}: slices differ from baseline")
            break
    if base_spec and spec.get("eval_set_version") == base_spec.get("eval_set_version"):
        old = {m["id"]: m for m in base_spec.get("metrics", [])}
        new = {m["id"]: m for m in spec.get("metrics", [])}
        for mid, previous in old.items():
            current = new.get(mid)
            if current is None:
                problems.append(f"ratchet: metric {mid} was removed")
                continue
            if previous.get("blocking") and not current.get("blocking"):
                problems.append(f"ratchet: metric {mid} no longer blocks")
            pth, cth = previous.get("threshold", {}), current.get("threshold", {})
            if "min" in pth and ("min" not in cth or cth["min"] < pth["min"]):
                problems.append(f"ratchet: metric {mid} min was lowered")
            if "max" in pth and ("max" not in cth or cth["max"] > pth["max"]):
                problems.append(f"ratchet: metric {mid} max was raised")
            if pth.get("no_significant_drop") and not cth.get("no_significant_drop"):
                problems.append(f"ratchet: metric {mid} removed no_significant_drop")
        old_slices = {(s["name"], s["metric"]): s for s in base_spec.get("slices", [])}
        new_slices = {(s["name"], s["metric"]): s for s in spec.get("slices", [])}
        for key, previous in old_slices.items():
            current = new_slices.get(key)
            label = f"slice:{key[0]}/{key[1]}"
            if current is None:
                problems.append(f"ratchet: {label} was removed")
                continue
            if previous.get("blocking") and not current.get("blocking"):
                problems.append(f"ratchet: {label} no longer blocks")
            if "min" in previous and ("min" not in current or current["min"] < previous["min"]):
                problems.append(f"ratchet: {label} min was lowered")
            if previous.get("no_significant_drop") and not current.get("no_significant_drop"):
                problems.append(f"ratchet: {label} removed no_significant_drop")
    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    bootstrap = spec.get("bootstrap", {})
    for rule in spec.get("slices", []):
        name, metric = rule["name"], rule["metric"]
        values = per_item(pr, metric, slice_name=name)
        base_values = per_item(base, metric, slice_name=name)
        value = sum(values.values()) / len(values) if values else None
        verdict, note = "ok", ""
        if value is None:
            verdict = "missing"
        elif "min" in rule and value < rule["min"]:
            verdict = "fail"
        elif "max" in rule and value > rule["max"]:
            verdict = "fail"
        if rule.get("no_significant_drop") and value is not None:
            keys = sorted(set(values) & set(base_values))
            if keys:
                delta, low, high = stats.paired_bootstrap(
                    [values[k] for k in keys], [base_values[k] for k in keys],
                    n_resamples=bootstrap.get("n_resamples", 1000), rng=bootstrap.get("rng", 2027),
                )
                note = f"Δ {delta:+.3f} CI [{low:+.3f}, {high:+.3f}]"
                if high < 0:
                    verdict = "fail"
        ident = f"slice:{name}/{metric}"
        if verdict != "ok" and rule.get("blocking"):
            blocked.append(ident)
        label = "ok" if verdict == "ok" else "MISSING" if verdict == "missing" else "BLOCK" if rule.get("blocking") else "warn"
        lines.append(f"  {ident:<22} {value!s:<8} {json.dumps({k: v for k, v in rule.items() if k not in ('name', 'metric')}):<40} {note:<28} {label}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
