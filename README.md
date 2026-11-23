# BOXX automation dashboard

The conversation tests stay in the product repo. This repo is the record people open.

GitHub Pages reads `data/latest.json` and `data/history.json`. A publish replaces those files. It does not change the page.

After a deploy check in the product repo:

```bash
make eval-conv-publish
```

The page then shows three different facts:

- **Broke since last run.** A journey that passed on the previous publish and failed on this one. That is an existing flow this change affected.
- **Still failing.** It was already red. This deploy did not introduce it.
- **Fixed since last run.** It was red and is now green.

The site is public because this repository is public. Publish synthetic journeys only. Do not publish a report that contains a real customer's chat.
