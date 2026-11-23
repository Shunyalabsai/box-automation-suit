#!/usr/bin/env python3
"""Read one or more conversation-eval reports and print what is still broken.

Exit 0 only when every case passed and the run had no harness error.
A deployment check uses that exit code. The text is the list to fix.

    python3 eval/synthetic_report.py --report eval/reports/synthetic_journeys-*.json
"""
from __future__ import annotations

import argparse
import json
import sys


FAMILIES = (
    ("Enquiry, nothing lost", "SYN-ENQ-"),
    ("Money or device already gone", "SYN-LOSS-"),
    ("Safety, not a scam", "SYN-SAFE-"),
    ("Second turn changes the answer", "SYN-FLOW-"),
    ("Not a fraud story", "SYN-X-"),
    ("Same loss, other languages", "SYN-LANG-"),
    ("Abroad and the helpline", "CL-AB-"),
    ("Claim", "CL-CLM-"),
    ("Is this message legitimate", "CL-LEG-"),
    ("UAT live scam", "UAT-EM-"),
    ("UAT check a message", "UAT-LK-"),
    ("UAT policy and claim", "UAT-CL-"),
    ("UAT family protection", "UAT-FM-"),
    ("UAT something else", "UAT-OT-"),
)


def family_of(case_id: str) -> str:
    for title, prefix in FAMILIES:
        if case_id.startswith(prefix):
            return title
    return "Other"


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    rows = []
    servers = []
    for path in args.report:
        report = load(path)
        servers.append(str(report.get("base") or ""))
        for result in report.get("results") or []:
            result["_suite"] = report.get("suite") or path
            rows.append(result)

    passed = [r for r in rows if r.get("passed") is True and not r.get("harness_error")]
    failed = [r for r in rows if r.get("passed") is not True or r.get("harness_error")]
    lines = []
    lines.append("DEPLOY CHECK")
    if servers:
        lines.append("server: " + ", ".join(s for s in servers if s))
    lines.append(
        f"cases {len(rows)}   pass {len(passed)}   fail {len(failed)}"
    )
    lines.append("")
    buckets: dict[str, list] = {title: [] for title, _ in FAMILIES}
    buckets["Other"] = []
    for row in rows:
        buckets.setdefault(family_of(row.get("id") or ""), []).append(row)
    order = [title for title, _ in FAMILIES] + ["Other"]
    for title in order:
        group = buckets.get(title) or []
        if not group:
            continue
        ok = sum(1 for r in group if r.get("passed") is True and not r.get("harness_error"))
        lines.append(f"{title}  {ok}/{len(group)}")
        for row in group:
            if row.get("passed") is True and not row.get("harness_error"):
                continue
            reason = row.get("error") or ", ".join(row.get("fails") or []) or "no verdict"
            lines.append(f"  FAIL  {row.get('id')}  {reason}")
        lines.append("")
    if not failed:
        lines.append("Nothing in this run is failing.")
    text = "\n".join(lines).rstrip() + "\n"
    sys.stdout.write(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
