#!/usr/bin/env python3
"""Write eval/suites/smoke_happy.jsonl from the client UAT sheet.

Fifteen happy-path journeys: one per numbered sheet row (there is no row 9),
plus the extra note under the table (father / new phone). Each case can carry
a follow-up user message so the smoke run is multi-turn where the story needs it.
"""
from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "suites" / "client_uat_sheet.jsonl"
OUT = HERE / "suites" / "smoke_happy.jsonl"

# One primary case per sheet row, in sheet order.
SMOKE_IDS = (
    "UAT-EM-01-family-accident-call-en",
    "UAT-EM-02-digital-arrest-customs-en",
    "UAT-EM-03-anydesk-code-en",
    "UAT-LK-04-bank-kyc-sms-hi",
    "UAT-LK-05-income-tax-refund-en",
    "UAT-LK-06-flipkart-lucky-draw-mr",
    "UAT-CL-07-policy-phishing-covered-en",
    "UAT-CL-08-physical-card-not-covered-en",
    "UAT-FM-10-elderly-parents-en",
    "UAT-FM-11-job-registration-fee-hi",
    "UAT-FM-12-matrimonial-money-mr",
    "UAT-OT-13-book-a-cab-en",
    "UAT-OT-14-vague-distress-hi",
    "UAT-OT-15-pm-kisan-mr",
    "UAT-FM-16-father-new-phone-en",
)

SHEET_ROW = {
    "UAT-EM-01-family-accident-call-en": "1",
    "UAT-EM-02-digital-arrest-customs-en": "2",
    "UAT-EM-03-anydesk-code-en": "3",
    "UAT-LK-04-bank-kyc-sms-hi": "4",
    "UAT-LK-05-income-tax-refund-en": "5",
    "UAT-LK-06-flipkart-lucky-draw-mr": "6",
    "UAT-CL-07-policy-phishing-covered-en": "7",
    "UAT-CL-08-physical-card-not-covered-en": "8",
    "UAT-FM-10-elderly-parents-en": "10",
    "UAT-FM-11-job-registration-fee-hi": "11",
    "UAT-FM-12-matrimonial-money-mr": "12",
    "UAT-OT-13-book-a-cab-en": "13",
    "UAT-OT-14-vague-distress-hi": "14",
    "UAT-OT-15-pm-kisan-mr": "15",
    "UAT-FM-16-father-new-phone-en": "16",
}

# Follow-up messages keep the same expectations on the whole conversation.
FOLLOW_UP = {
    "UAT-EM-01-family-accident-call-en": (
        "He is calling again from another number. They want ₹50,000 in the next ten minutes. Should I transfer?",
    ),
    "UAT-EM-02-digital-arrest-customs-en": (
        "The call is still on. They are shouting and say I will be arrested if I hang up.",
    ),
    "UAT-LK-04-bank-kyc-sms-hi": (
        "I opened the link but did not enter the OTP. What should I do now?",
    ),
    "UAT-CL-07-policy-phishing-covered-en": (
        "Yes, ₹18,000 already left my account. What is the first step I should take for the claim?",
    ),
    "UAT-FM-10-elderly-parents-en": (
        "They got another call today from someone claiming to be police. What should they do right now?",
    ),
    "UAT-OT-14-vague-distress-hi": (
        "मुझे लगता है किसी ने मेरे खाते से पैसे निकाले हैं, पर पूरा यकीन नहीं है।",
    ),
    "UAT-FM-16-father-new-phone-en": (
        "He got a WhatsApp link to complete KYC. Should he open it?",
    ),
}


def load_cases() -> dict[str, dict]:
    cases = {}
    for line in SRC.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        cases[case["id"]] = case
    return cases


def normalise_turns(turns: list) -> list:
    out = []
    for turn in turns:
        if isinstance(turn, str):
            out.append(turn)
        elif isinstance(turn, dict):
            out.append(turn)
        else:
            out.append(str(turn))
    return out


def build() -> list[dict]:
    catalog = load_cases()
    missing = [cid for cid in SMOKE_IDS if cid not in catalog]
    if missing:
        raise SystemExit("missing from client_uat_sheet.jsonl: " + ", ".join(missing))
    rows = []
    for cid in SMOKE_IDS:
        case = json.loads(json.dumps(catalog[cid]))
        case["source"] = "smoke-uat-happy"
        case["sheet_row"] = SHEET_ROW[cid]
        turns = normalise_turns(case.get("turns") or [])
        for extra in FOLLOW_UP.get(cid, ()):
            turns.append(extra)
        case["turns"] = turns
        case["smoke_title"] = case["id"].replace("UAT-", "").rsplit("-", 1)[0].replace("-", " ")
        rows.append(case)
    return rows


def main() -> int:
    rows = build()
    OUT.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} cases -> {OUT}")
    multi = sum(1 for row in rows if len(row.get("turns") or []) > 1)
    print(f"multi-turn journeys: {multi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())