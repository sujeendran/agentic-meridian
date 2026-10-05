#!/usr/bin/env python3
"""Build a Playwright MCP step script: helpers + recording start time + step body.

The result is an `async (page) => { ... }` file for `browser_run_code_unsafe(filename=...)`.
Every step needs T0 baked in because nothing persists between MCP code calls.

    python make_step.py --t0 1791191067057 --out .playwright-mcp/output/demo/s1.js < body.js
    python make_step.py --t0 1791191067057 --out .playwright-mcp/output/demo/s1.js --body body.js
    python make_step.py --retime 1791199999999 .playwright-mcp/output/demo/*.js   # new take, same steps
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HELPERS = Path(__file__).resolve().parent.parent / "assets" / "step-helpers.js"


def build(t0: int, body: str) -> str:
    helpers = HELPERS.read_text(encoding="utf-8").replace("__T0__", str(t0))
    return f"async (page) => {{\n{helpers}\n{body.rstrip()}\n}}\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--t0", type=int, help="recording start time, epoch milliseconds")
    ap.add_argument("--out", type=Path, help="step file to write (inside the workspace)")
    ap.add_argument("--body", type=Path, help="file with the step body (default: stdin)")
    ap.add_argument("--retime", type=int, metavar="T0", help="rewrite T0 in existing step files")
    ap.add_argument("files", nargs="*", type=Path, help="step files for --retime")
    args = ap.parse_args()

    if args.retime:
        for f in args.files:
            text = f.read_text(encoding="utf-8")
            new = re.sub(r"const T0 = \d+;", f"const T0 = {args.retime};", text, count=1)
            f.write_text(new, encoding="utf-8")
            print(f"retimed {f}")
        return
    if args.t0 is None or args.out is None:
        ap.error("--t0 and --out are required (or use --retime)")
    body = args.body.read_text(encoding="utf-8") if args.body else sys.stdin.read()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(args.t0, body), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
