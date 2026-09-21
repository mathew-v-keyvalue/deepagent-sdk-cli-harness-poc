"""Unified eval CLI.

    python -m eval run --target local|netra|both [--gate] [options]

A thin dispatcher over the modules built for each track — no scoring or
request logic lives here. See `eval/README.md` for what each track actually
does and what's parked.

Examples:

    # Local track only, quick smoke run
    python -m eval run --target local --limit 10

    # Local track (full dataset) plus the confirm-gate pause-only check
    python -m eval run --target local --gate

    # Both tracks (assumes eval.netra.setup_dataset has been run once, and
    # the library evaluators have been created per eval/netra/EVALUATOR_SETUP.md)
    python -m eval run --target both
"""

from __future__ import annotations

import argparse
import sys


def _run_local(args: argparse.Namespace) -> int:
    from eval.local import runner, score

    runner_argv = ["--base-url", args.base_url, "--categories", args.categories, "--concurrency", str(args.concurrency)]
    if args.limit is not None:
        runner_argv += ["--limit", str(args.limit)]
    if args.service_auth:
        runner_argv += ["--service-auth", args.service_auth]
    if args.all_examples:
        runner_argv.append("--all-examples")
    if args.resume:
        runner_argv.append("--resume")

    rc = runner.main(runner_argv)
    score_rc = score.main([])
    return rc or score_rc


def _run_gate(args: argparse.Namespace) -> int:
    from eval.local import gate_runner

    gate_argv = ["--base-url", args.base_url]
    if args.service_auth:
        gate_argv += ["--service-auth", args.service_auth]
    return gate_runner.main(gate_argv)


def _run_netra(args: argparse.Namespace) -> int:
    del args  # eval.netra.run takes no CLI options today — see its own module
    from eval.netra import run as netra_run

    return netra_run.main([])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    run_parser = subparsers.add_parser("run", help="Run one or more eval tracks")
    run_parser.add_argument("--target", choices=["local", "netra", "both"], default="local", help="Which track(s) to run (default: %(default)s)")
    run_parser.add_argument("--gate", action="store_true", help="Also run the confirm-gate pause-only check (local target only — see eval/local/gate_runner.py)")
    run_parser.add_argument("--base-url", default="http://localhost:8000", help="Already-running /chat server (default: %(default)s) — local target and --gate only")
    run_parser.add_argument("--categories", default="single,composite,adversarial,auth", help="Local target only — comma-separated subset of: single, composite, adversarial, auth")
    run_parser.add_argument("--limit", type=int, default=None, help="Local target only — stop after N queries (quick smoke run)")
    run_parser.add_argument("--all-examples", action="store_true", help="Local target only — run every example_queries[] variant, not just the first")
    run_parser.add_argument("--resume", action="store_true", help="Local target only — skip queries already present in the results file")
    run_parser.add_argument("--concurrency", type=int, default=1, help="Local target only — run up to N queries at once (default: 1, serial — see eval/local/runner.py's 'Concurrency' section)")
    run_parser.add_argument("--service-auth", default=None, help="X-Service-Auth value (default: $DEEPAGENT_SERVICE_AUTH) — local target and --gate only")

    args = parser.parse_args(argv)

    if args.subcommand != "run":
        return 0  # unreachable — only one subcommand exists today

    overall_rc = 0
    if args.target in ("local", "both"):
        overall_rc = overall_rc or _run_local(args)
        if args.gate:
            overall_rc = overall_rc or _run_gate(args)
    if args.target in ("netra", "both"):
        overall_rc = overall_rc or _run_netra(args)

    return overall_rc


if __name__ == "__main__":
    sys.exit(main())
