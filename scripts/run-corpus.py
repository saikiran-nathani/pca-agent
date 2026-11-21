#!/usr/bin/env python3
"""Run the exploit corpus against this project's executor and print what held.

    scripts/run-corpus.py [--reps N] [--out PATH] [--case ID ...]

Exit status is 0 only when the controls behaved AND every exploit scored zero on every
repetition. Any other combination exits non-zero, including the one where the controls
themselves misbehaved -- an all-zero corpus from a harness that cannot execute anything looks
exactly like total success, and this script refuses to report it as such.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pca.audit.corpus import BY_ID, CASES  # noqa: E402
from pca.audit.score import (  # noqa: E402
    accepted,
    controls_are_sound,
    format_table,
    pca_executor,
    run_corpus,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=25, help="repetitions per case (default 25)")
    parser.add_argument("--out", default="var/corpus-report.json", help="where to write the JSON record")
    parser.add_argument("--case", action="append", default=[], metavar="ID", help="run only these case ids")
    parser.add_argument("--mode", choices=("snippet", "check"), default="snippet", help="which test form to score against (default snippet, the weaker one)")
    args = parser.parse_args()

    # A --case run still carries the controls. Without them the script has to print
    # "corpus unreadable" and exit 2 over a selection the user made deliberately, and a
    # diagnostic that refuses to diagnose is just an obstacle.
    cases = list(CASES)
    if args.case:
        chosen = set(args.case)
        cases = [c for c in CASES if c.kind == "control" or c.id in chosen]
    missing = [c for c in args.case if c not in BY_ID]
    if missing:
        print(f"unknown case id(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    print(f"corpus: {len(cases)} cases x {args.reps} reps, {args.mode} mode, on {platform.system()} {platform.machine()}")
    started = time.monotonic()
    reports = run_corpus(pca_executor(mode=args.mode), reps=args.reps, cases=cases, mode=args.mode)
    elapsed = time.monotonic() - started

    print()
    print(format_table(reports))
    print()

    sound, control_detail = controls_are_sound(reports)
    holes = accepted(reports)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "reps": args.reps,
                "mode": args.mode,
                "elapsed_s": round(elapsed, 1),
                "controls_sound": sound,
                "controls_detail": control_detail,
                "accepted": [r.id for r in holes],
                "cases": [r.to_dict() for r in reports],
            },
            indent=2,
        )
        + "\n"
    )
    print(f"controls: {control_detail}")
    print(f"record:   {out}")

    if not sound:
        print("\nCORPUS UNREADABLE: the controls did not behave. Fix the harness before reading", file=sys.stderr)
        print("anything else here -- a broken executor scores every exploit zero too.", file=sys.stderr)
        return 2

    if holes:
        print(f"\nSTOP THE LINE: {len(holes)} of {sum(1 for r in reports if r.kind == 'exploit')} exploits were accepted.", file=sys.stderr)
        for r in holes:
            case = BY_ID[r.id]
            print(f"  {r.id} {r.name:<16} scored {r.worst:.3f} on {len(r.fractions) - r.held_count}/{len(r.fractions)} reps -- {case.mechanism}", file=sys.stderr)
        print("\nPhase 1.3: any exploit above zero blocks everything downstream.", file=sys.stderr)
        return 1

    print(f"\ngate ok: {sum(1 for r in reports if r.kind == 'exploit')} exploits, {args.reps} reps each, every one scored zero.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
