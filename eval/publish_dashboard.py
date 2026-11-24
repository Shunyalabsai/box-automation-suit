#!/usr/bin/env python3
"""Publish the latest conversation-test reports to the dashboard repo.

The tests stay in this repo. GitHub Pages on
https://github.com/Shunyalabsai/box-automation-suit only stores the result,
so a person can open one page and see what failed.

A case that passed on the previous publish and fails on this one is marked
regression: an existing flow this run affected. A case that was already
failing stays "still failing".

    python3 eval/publish_dashboard.py                      # write eval/reports/dashboard/ only
    python3 eval/publish_dashboard.py --push               # and push it to the dashboard repo
    python3 eval/publish_dashboard.py --report eval/reports/synthetic_journeys-*.json
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORTS = ROOT / "eval" / "reports"
SUITES = ROOT / "eval" / "suites"
REMOTE = os.environ.get(
    "BOXX_DASHBOARD_REPO",
    "https://github.com/Shunyalabsai/box-automation-suit.git",
)

LABELS = {
    "smoke_happy": "UAT happy paths (15)",
    "synthetic_journeys": "Full journey catalogue",
    "client_2026_11_23": "23 Nov client screenshots",
    "client_uat_sheet": "Client UAT sheet",
}

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


def environment_of(server: str) -> str:
    explicit = os.environ.get("BOXX_EVAL_ENV", "").strip().lower()
    if explicit in ("staging", "stage", "stg"):
        return "staging"
    if explicit in ("production", "prod", "live"):
        return "production"
    host = str(server or "").lower()
    if "staging" in host or "boxx-staging" in host:
        return "staging"
    if "boxxv3" in host or ":8200" in host:
        return "production"
    return "other"


def environment_label(name: str) -> str:
    return {
        "staging": "Staging",
        "production": "Production",
        "other": "Other host",
    }.get(name, name or "Unknown")


def family_of(case_id: str) -> str:
    for title, prefix in FAMILIES:
        if case_id.startswith(prefix):
            return title
    return "Other"


def newest(*patterns: str) -> list[pathlib.Path]:
    found = []
    for pattern in patterns:
        matches = sorted(REPORTS.glob(pattern), key=lambda path: path.stat().st_mtime)
        if matches:
            found.append(matches[-1])
    return found


def clip(text: str, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def suite_notes() -> dict[str, dict]:
    notes = {}
    for path in SUITES.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(case, dict):
                continue
            expect = case.get("expect") if isinstance(case.get("expect"), dict) else {}
            watch = ""
            for crit in expect.get("criteria") or []:
                if isinstance(crit, dict) and crit.get("watch"):
                    watch = str(crit["watch"])
                    break
            notes[case.get("id") or ""] = {
                "note": case.get("note") or "",
                "watch": watch,
                "expect": expect,
                "sheet_row": case.get("sheet_row") or "",
                "smoke_title": case.get("smoke_title") or "",
            }
    return notes


def expected_behaviour(watch: str, note: str) -> str:
    """The behaviour a developer should read. Sheet row numbers are not that behaviour."""
    text = (watch or note or "").strip()
    text = re.sub(r"^Sheet case \d+\.\s*", "", text)
    text = re.sub(r"^UAT sheet row \d+[^.]*\.\s*", "", text)
    return text


def _matching_paren(text: str, open_at: int) -> int:
    depth = 0
    i = open_at
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_alt(body: str) -> list[str]:
    parts = []
    start = 0
    depth = 0
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == "(":
            depth += 1
        elif body[i] == ")":
            depth -= 1
        elif body[i] == "|" and depth == 0:
            parts.append(body[start:i])
            start = i + 1
        i += 1
    parts.append(body[start:])
    return [part for part in parts if part.strip()]


def _plain(text: str) -> str:
    text = text.replace("\\.", ".").replace("\\s+", " ").replace("\\s", " ")
    text = text.replace("(?:", "").replace("(", "").replace(")", "").replace("?", "")
    return re.sub(r"\s+", " ", text).strip(" |")


def _phrases(body: str, limit: int = 8) -> list[str]:
    """Turn one required-part pattern into the phrases a developer can look for."""
    near = re.match(
        r"^\(\?:([^()]+)\)\.\{(\d+),(\d+)\}\(\?:([^()]+)\)$",
        body.strip(),
    )
    if near:
        left = [_plain(part) for part in _split_alt(near.group(1))]
        right = [_plain(part) for part in _split_alt(near.group(4))]
        return [f'"{a}" within a few words of "{b}"' for a in left for b in right if a and b][:limit]
    body = re.sub(r"^\?:", "", body.strip())
    if body.startswith("(?:"):
        close = _matching_paren(body, 0)
        if close == len(body) - 1:
            body = body[3:-1]
    options = _split_alt(body) or [body]
    phrases = []
    for option in options:
        expanded = [option]
        while True:
            nxt = []
            grew = False
            for item in expanded:
                match = re.search(r"\((?:\?:)?([^()]+)\)", item)
                if not match or "|" not in match.group(1):
                    nxt.append(item)
                    continue
                grew = True
                for alt in _split_alt(match.group(1)):
                    nxt.append(item[: match.start()] + alt + item[match.end() :])
            expanded = nxt
            if not grew:
                break
        for item in expanded:
            text = _plain(item)
            if text and text not in phrases:
                phrases.append(text)
            if len(phrases) >= limit:
                return phrases
    return phrases


def _required_parts(pattern: str) -> list[str]:
    parts = []
    i = 0
    while True:
        start = pattern.find("(?=", i)
        if start < 0:
            break
        close = _matching_paren(pattern, start)
        if close < 0:
            break
        body = pattern[start + 3 : close]
        if body.startswith(".*"):
            body = body[2:]
        parts.append(body)
        i = close + 1
    if parts:
        return parts
    return [pattern]


def reply_text(row: dict) -> str:
    chunks = []
    for turn in row.get("turns") or []:
        if isinstance(turn, dict):
            chunks.extend(str(reply) for reply in (turn.get("replies") or []))
    text = "\n".join(chunks)
    try:
        sys_path = str(ROOT / "eval")
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from run_partner_p0 import normalise_digits, strip_boilerplate
        return strip_boilerplate(normalise_digits(text))
    except Exception:
        return text


def gap_for(expect: dict, row: dict, fails: list[str]) -> dict:
    """What the case required, which of that the reply did not contain, and the rule used."""
    blob = reply_text(row)
    missing = []
    basis = []
    if pattern := str(expect.get("must_match") or ""):
        missed_part = False
        for part in _required_parts(pattern):
            try:
                found = re.search(part, blob, re.I | re.S)
            except re.error:
                found = True
            if found:
                continue
            missed_part = True
            phrases = _phrases(part)
            if phrases:
                missing.append("None of these were in the reply: " + "; ".join(phrases) + ".")
            else:
                missing.append("A required part of the reply was missing.")
        if missed_part:
            basis.append(
                "The whole reply is checked. Each required part must appear. "
                "A part passes when any one of its phrases appears. Case does not matter."
            )
    if pattern := str(expect.get("must_not_match") or ""):
        try:
            hit = re.search(pattern, blob, re.I | re.S)
        except re.error:
            hit = None
        if hit and any(code.startswith("FORBIDDEN_CONTENT") or code.startswith("STALE_CONTEXT") for code in fails):
            basis.append("The reply must not contain a phrase from the forbidden list.")
            missing.append("The reply contains this, and it must not: " + hit.group(0).strip() + ".")
    if expect.get("urgent") and any(code.startswith("NO_URGENT_ACTION") for code in fails):
        basis.append(
            "An emergency must include an immediate step: hang up, call the bank, "
            "block or freeze, or 1930."
        )
        missing.append("No immediate step was in the reply.")
    if expect.get("mechanism") and any(code.startswith("WRONG_MECHANISM") for code in fails):
        named = ", ".join(re.findall(r"WRONG_MECHANISM\[([^\]]+)\]", " ".join(fails)))
        basis.append(
            "The reply must not describe a different scam from " + str(expect.get("mechanism")) + "."
        )
        if named:
            missing.append("The reply described this as " + named + ".")
    for code in fails:
        if code.startswith("NO_AUTHORED_PLAN"):
            basis.append("A confirmed loss must reach the recovery plan, the numbered steps.")
            missing.append("The reply never started that plan.")
        elif code.startswith("NO_TEL_CTA"):
            basis.append("Saying to call only counts when the message also has a call button.")
            missing.append("There was no call button.")
        elif code.startswith("LOST_EARLIER_CONTEXT"):
            basis.append("The last reply is checked on its own for a fact the conversation already established.")
            missing.append("That fact was not in the last reply.")
        elif code.startswith("RE_ASKED"):
            basis.append("Once the user has done a step, a later reply must not ask for that step again.")
            missing.append("A later reply asked for a step the user had already done.")
    # Drop duplicate sentences. A case can carry both a phrase miss and an emergency miss.
    unique = []
    for line in missing:
        if line not in unique:
            unique.append(line)
    unique_basis = []
    for line in basis:
        if line not in unique_basis:
            unique_basis.append(line)
    return {"missing": unique, "basis": " ".join(unique_basis)}


def turns_of(result: dict) -> list[dict]:
    out = []
    for turn in result.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        user = turn.get("user")
        if isinstance(user, dict):
            user = user.get("text") or ""
        replies = [clip(reply, 1200) for reply in (turn.get("replies") or [])[:6]]
        out.append({"user": clip(user or "", 800), "replies": replies})
    return out[:16]


def status_for(case_id: str, passed: bool, previous: dict[str, bool]) -> str:
    if case_id not in previous:
        return "pass" if passed else "new_fail"
    was = previous[case_id]
    if was and not passed:
        return "regression"
    if not was and not passed:
        return "still_failing"
    if not was and passed:
        return "fixed"
    return "pass"


def build(report_paths: list[pathlib.Path], previous: dict[str, bool]) -> dict:
    notes = suite_notes()
    results = []
    servers = []
    for path in report_paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("base"):
            servers.append(str(report["base"]))
        suite = report.get("suite") or path.name.split("-")[0]
        for row in report.get("results") or []:
            case_id = str(row.get("id") or "")
            passed = row.get("passed") is True and not row.get("harness_error")
            fails = list(row.get("fails") or [])
            if row.get("harness_error"):
                fails = fails or ["HARNESS_ERROR"]
            meta = notes.get(case_id) or {}
            expect = meta.get("expect") if isinstance(meta.get("expect"), dict) else {}
            gap = gap_for(expect, row, fails)
            results.append({
                "id": case_id,
                "suite": suite,
                "family": family_of(case_id),
                "sheet_row": meta.get("sheet_row") or "",
                "title": meta.get("smoke_title") or "",
                "turn_count": len(row.get("turns") or []),
                "passed": passed,
                "status": status_for(case_id, passed, previous),
                "fails": fails,
                "expected": expected_behaviour(meta.get("watch") or "", meta.get("note") or ""),
                "missing": gap["missing"],
                "basis": gap["basis"],
                "turns": turns_of(row),
            })
    order = {"regression": 0, "new_fail": 1, "still_failing": 2, "fixed": 3, "pass": 4}
    results.sort(key=lambda row: (order.get(row["status"], 9), row["family"], row["id"]))
    counts = {
        "cases": len(results),
        "pass": sum(1 for row in results if row["passed"]),
        "fail": sum(1 for row in results if not row["passed"]),
        "regression": sum(1 for row in results if row["status"] == "regression"),
        "still_failing": sum(1 for row in results if row["status"] in ("still_failing", "new_fail")),
        "fixed": sum(1 for row in results if row["status"] == "fixed"),
    }
    names = []
    for path in report_paths:
        stem = path.name.split("-")[0]
        names.append(LABELS.get(stem, stem))
    server = ", ".join(dict.fromkeys(servers))
    environment = environment_of(server)
    env_tag = environment_label(environment)
    title = ", ".join(dict.fromkeys(names))
    return {
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": f"{env_tag} · {title}",
        "environment": environment,
        "environment_label": env_tag,
        "server": server,
        "reports": [path.name for path in report_paths],
        "counts": counts,
        "results": results,
    }


def previous_verdicts(path: pathlib.Path) -> dict[str, bool]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {row["id"]: bool(row.get("passed")) for row in data.get("results") or [] if row.get("id")}


def run(cmd: list[str], cwd: pathlib.Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def publish(payload: dict, checkout: pathlib.Path, push: bool) -> None:
    data = checkout / "data"
    data.mkdir(exist_ok=True)
    latest = data / "latest.json"
    history_path = data / "history.json"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else []
    except json.JSONDecodeError:
        history = []
    if not isinstance(history, list):
        history = []
    counts = payload["counts"]
    history.append({
        "published_at": payload["published_at"],
        "environment": payload.get("environment") or "",
        "environment_label": payload.get("environment_label") or "",
        "label": payload.get("label") or "",
        "server": payload.get("server") or "",
        "pass": counts["pass"],
        "fail": counts["fail"],
        "regression": counts["regression"],
        "fixed": counts["fixed"],
    })
    history = history[-40:]
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if not push:
        print(f"wrote {latest}")
        print(f"fail {counts['fail']}  broke {counts['regression']}  fixed {counts['fixed']}")
        return
    run(["git", "add", "data/latest.json", "data/history.json"], checkout)
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=checkout)
    if status.returncode == 0:
        print("dashboard already has this result")
        return
    message = (
        f"Publish test run {payload['published_at']}: "
        f"{counts['fail']} failing, {counts['regression']} broke since last run."
    )
    run(["git", "commit", "-m", message], checkout)
    run(["git", "push", "origin", "HEAD"], checkout)
    print("published", payload["published_at"])
    print(f"fail {counts['fail']}  broke {counts['regression']}  fixed {counts['fixed']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", default=[])
    parser.add_argument("--checkout", default="")
    # OPT-IN (24 Nov): publishing sends the client's test cases and the bot's replies to the dashboard repo, so it
    # happens only when asked for. Without --push the page data is written to eval/reports/dashboard/ only.
    parser.add_argument("--push", action="store_true",
                        help="push the result to the dashboard repo (BOXX_DASHBOARD_REPO); otherwise write it locally")
    args = parser.parse_args()
    paths = [pathlib.Path(item) for item in args.report] or newest(
        "smoke_happy-*.json")
    if not paths:
        raise SystemExit("no reports in eval/reports/. Run make eval-conv-synthetic first.")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit("missing report: " + ", ".join(missing))

    own_checkout = pathlib.Path(args.checkout) if args.checkout else None
    if not own_checkout and (ROOT / "index.html").is_file():
        own_checkout = ROOT
    if not own_checkout and not args.push:
        own_checkout = ROOT / "eval" / "reports" / "dashboard"
        own_checkout.mkdir(parents=True, exist_ok=True)
    if own_checkout:
        payload = build(paths, previous_verdicts(own_checkout / "data" / "latest.json"))
        publish(payload, own_checkout, push=args.push)
        return 0

    with tempfile.TemporaryDirectory(prefix="box-dashboard-") as tmp:
        checkout = pathlib.Path(tmp) / "repo"
        subprocess.run(["git", "clone", "--depth", "1", REMOTE, str(checkout)], check=True)
        payload = build(paths, previous_verdicts(checkout / "data" / "latest.json"))
        publish(payload, checkout, push=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
