# BOXX automation

This repository is the AI conversation test suite and the page that shows the last run.

https://shunyalabsai.github.io/box-automation-suit/

The cases are synthetic, or they were written from the client's own test sheet and screenshots. Do not add a real customer's chat.

## Run it

You need Python 3, network access to the test server, and the test API key. The key is not stored in this repository.

```
export TEST_API_KEY=the-key-from-the-team
make smoke
```

`make smoke` runs ten journeys and takes about five minutes. It is the quick go-ahead check.

`make full` runs the whole catalogue, about 136 conversations, and takes about an hour.

The default server is `https://boxxv3.shunyalabs.ai`. Set `BOXX_TEST_BASE` to point a run somewhere else.

## What the page shows

The page updates about a minute after a run finishes. The upload takes a few seconds, then GitHub Pages rebuilds. Updating the page needs permission to push this repository. Opening the page does not.

Each failure shows three things:

- **Expected behaviour.** What a correct reply has to do.
- **Missing in the bot response.** The part that was not there.
- **Basis of the check.** The rule that decided the case.

A journey that passed on the previous publish and fails on this one is marked **Broke since last run**. That is an existing flow this run affected. A journey that was already red stays **Still failing**. A journey that was red and is now green is **Fixed since last run**.

## What is in the catalogue

- `eval/suites/smoke_happy.jsonl` — the ten-journey go-ahead.
- `eval/suites/synthetic_journeys.jsonl` — one conversation per journey the product claims to handle.
- `eval/suites/client_2026_11_23.jsonl` — the 23 Nov client screenshots.
- `eval/suites/client_uat_sheet.jsonl` — the client UAT sheet, including the policy-image cases.
- `BOXX_CyberCare_UAT_Test_Cases.xlsx` — the sheet those UAT cases come from.

`python3 eval/build_synthetic_journeys.py` rewrites the synthetic file. `python3 eval/build_synthetic_journeys.py --check` checks it without writing.
