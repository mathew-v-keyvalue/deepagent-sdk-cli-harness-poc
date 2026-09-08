"""Score an existing `eval/local/runner.py` results capture against
`eval/local/dataset.json`'s `expected`/`expected_commands`, using
`eval/local/scoring.py`'s pure functions — no server, no Netra, no network
access needed. This is the fast, Netra-independent sanity check: it reads
exactly the same SSE-derived `tool_calls` a Netra `tool_accuracy` evaluator
would see via `trace.tools`, so if the two disagree the trace pipeline (not
the underlying agent behavior) is the suspect — see `eval/README.md`'s "Two
scoring paths" section.

Usage:

    python -m eval.local.runner --service-auth ...   # produces results.json
    python -m eval.local.score                       # scores results.json

    python -m eval.local.score --input eval/results/results.json \\
        --output eval/results/local_score_summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from eval.local.scoring import aggregate, check_no_internal_leakage, extract_actual_commands, score_single

DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "results" / "results.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "results" / "local_score_summary.json"


def _expected_commands_for(category: str, expected: dict[str, Any] | None) -> list[str]:
    """Map a result's `expected` field (shape varies by category — see
    `_iter_dataset_queries` in `eval/local/runner.py`) to the flat list of
    expected command strings `score_single` compares against. Adversarial
    queries have no manifest command behind them by construction, so they
    always map to `[]` (nothing should be invoked)."""
    expected = expected or {}
    if category == "single_command_queries":
        command = expected.get("command")
        return [command] if command else []
    if category in ("composite_queries", "auth_scenarios"):
        return list(expected.get("expected_commands") or [])
    if category == "adversarial_queries":
        return []
    return []


def score_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a `"score"` key (from `scoring.score_single`) to every result
    that isn't a run failure. Errored results are left unscored — see
    `scoring.aggregate`'s docstring for why they're excluded from
    aggregation rather than counted as a zero-command miss.

    `auth_scenarios` entries whose `expected.expected_behavior` is
    `"plain_language_reauth"` (the invalid-token case) also get a
    `"leak_check"` key — command precision/recall alone can't catch "invoked
    the right command, but leaked an internal auth mechanism name in the
    final answer instead of asking the user to sign in again"."""
    scored_results = []
    for result in results:
        result = dict(result)
        if not result.get("error"):
            actual = extract_actual_commands(result.get("tool_calls") or [])
            expected = _expected_commands_for(result["category"], result.get("expected"))
            result["score"] = score_single(actual, expected)
            if result["category"] == "auth_scenarios" and (result.get("expected") or {}).get("expected_behavior") == "plain_language_reauth":
                result["leak_check"] = check_no_internal_leakage(result.get("answer") or "")
        scored_results.append(result)
    return scored_results


def _print_table(summary: dict[str, Any]) -> None:
    print(f"\nScored {summary['scored_results']}/{summary['total_results']} result(s) "
          f"({summary['errored_results']} errored, excluded from scoring).\n")

    header = f"{'category':<24} {'n':>4} {'micro P':>8} {'micro R':>8} {'micro F1':>9} {'rejection':>10}"
    print(header)
    print("-" * len(header))
    for category, stats in summary["by_category"].items():
        micro = stats["micro"]
        rejection = stats["correct_rejection_rate"]
        print(
            f"{category:<24} {stats['count']:>4} "
            f"{_fmt(micro['precision']):>8} {_fmt(micro['recall']):>8} {_fmt(micro['f1']):>9} "
            f"{_fmt(rejection):>10}"
        )

    overall_micro = summary["micro"]
    overall_macro = summary["macro"]
    print("-" * len(header))
    print(
        f"{'OVERALL (micro)':<24} {summary['scored_results']:>4} "
        f"{_fmt(overall_micro['precision']):>8} {_fmt(overall_micro['recall']):>8} {_fmt(overall_micro['f1']):>9} "
        f"{_fmt(summary['adversarial_correct_rejection_rate']):>10}"
    )
    print(
        f"{'OVERALL (macro)':<24} {summary['scored_results']:>4} "
        f"{_fmt(overall_macro['precision']):>8} {_fmt(overall_macro['recall']):>8} {_fmt(overall_macro['f1']):>9} {'':>10}"
    )

    rating = rate_score(overall_micro["f1"])
    if rating:
        print(f"\nOverall rating: {rating['label']} (micro F1 {_fmt(overall_micro['f1'])}) — {rating['meaning']}")

    latency = summary["latency_seconds"]
    print(f"\nLatency: p50={_fmt(latency['p50'])}s  p95={_fmt(latency['p95'])}s")

    leak_checks = summary.get("leak_checks") or []
    if leak_checks:
        print("\nAuth leak checks (invalid-token scenarios — must never name internal auth mechanics):")
        for entry in leak_checks:
            status = "FAIL (leaked)" if entry["leaked"] else ("ok" if entry["reauth_hint_present"] else "WARN (no re-auth hint)")
            print(f"  [{status}] {entry['dataset_id']}" + (f" -- leaked: {entry['leaked_terms']}" if entry["leaked"] else ""))

    _print_legend()


# Qualitative bands over F1 (the single balanced number) — a common rule of
# thumb for classification-style metrics, not an official standard or a
# pass/fail gate. Given here so a non-technical viewer gets a plain-language
# read alongside the raw number, not just "0.857" with no context for what
# that's supposed to mean.
_RATING_BANDS = (
    (0.9, "Excellent", "the model is invoking essentially the right command(s) every time"),
    (0.7, "Good", "mostly correct, with occasional wrong or missing commands"),
    (0.5, "Fair", "right about half the time — a real gap worth investigating"),
    (0.0, "Poor", "wrong or incomplete more often than not"),
)


def rate_score(f1: float | None) -> dict[str, str] | None:
    """Map an F1 score to a qualitative label + plain-language meaning —
    None if there's nothing scorable (f1 is None, e.g. an all-adversarial or
    all-errored run)."""
    if f1 is None:
        return None
    for threshold, label, meaning in _RATING_BANDS:
        if f1 >= threshold:
            return {"label": label, "meaning": meaning}
    return None


_LEGEND_TEXT = """
What these numbers mean:
  Precision — of the commands the model actually ran, what fraction were
              actually correct/expected. Low precision = it ran extra or
              wrong commands alongside (or instead of) the right one.
  Recall    — of the commands that SHOULD have been run, what fraction the
              model actually ran. Low recall = it missed something it
              should have done.
  F1        — the balance of precision and recall into one number (their
              harmonic mean) — the single best "how good overall" figure
              if you only look at one column.
  rejection — for adversarial queries only (requests with no real backend
              capability behind them): the fraction the model correctly
              refused instead of inventing an answer. 1.0 = never
              hallucinated a capability that doesn't exist.
  Rough read on F1: >=0.9 Excellent, 0.7-0.9 Good, 0.5-0.7 Fair, <0.5 Poor
  (a common rule of thumb, not an official pass/fail threshold)."""


def _print_legend() -> None:
    print(_LEGEND_TEXT)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Results capture to score (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Where to write the score summary JSON")
    args = parser.parse_args(argv)

    if not args.input.exists():
        parser.error(f"{args.input} does not exist — run `python -m eval.local.runner` first")

    capture = json.loads(args.input.read_text())
    results = capture["results"]
    scored_results = score_results(results)
    summary = aggregate(scored_results)
    summary["source"] = str(args.input)
    summary["generated_at"] = capture.get("generated_at")
    summary["leak_checks"] = [
        {"dataset_id": r["dataset_id"], **r["leak_check"]} for r in scored_results if "leak_check" in r
    ]
    summary["overall_rating"] = rate_score(summary["micro"]["f1"])
    summary["legend"] = {
        "precision": "Of the commands actually run, what fraction were correct/expected.",
        "recall": "Of the commands that should have been run, what fraction actually were.",
        "f1": "Balance of precision and recall into one number — the best single 'how good overall' figure.",
        "rejection": "Adversarial queries only: fraction correctly refused instead of hallucinating a nonexistent capability.",
        "rating_bands": "F1 >=0.9 Excellent, 0.7-0.9 Good, 0.5-0.7 Fair, <0.5 Poor (a common rule of thumb, not an official threshold).",
    }

    _print_table(summary)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2))
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
