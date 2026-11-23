.PHONY: smoke full publish

STAMP := $(shell date -u +%Y%m%dT%H%M%SZ)

# Ten journeys. About five minutes once the suite lock is free.
# Exit 1 when any of them fails. The page is updated before that exit.
smoke:
	@mkdir -p eval/reports
	@python3 eval/run_partner_p0.py --suite eval/suites/smoke_happy.jsonl \
	  --cooldown 5 \
	  --out eval/reports/smoke_happy-$(STAMP).json
	@python3 eval/publish_dashboard.py --report eval/reports/smoke_happy-$(STAMP).json
	@python3 eval/synthetic_report.py \
	  --report eval/reports/smoke_happy-$(STAMP).json \
	  --out eval/reports/smoke-summary-$(STAMP).txt

# The full catalogue: synthetic journeys, the 23 Nov client screenshots, and the
# client UAT sheet. About 136 conversations. Budget one hour.
full:
	@mkdir -p eval/reports
	@python3 eval/run_partner_p0.py --suite eval/suites/synthetic_journeys.jsonl \
	  --out eval/reports/synthetic_journeys-$(STAMP).json
	@python3 eval/run_partner_p0.py --suite eval/suites/client_2026_11_23.jsonl \
	  --out eval/reports/client_2026_11_23-$(STAMP).json
	@python3 eval/run_partner_p0.py --suite eval/suites/client_uat_sheet.jsonl \
	  --out eval/reports/client_uat_sheet-$(STAMP).json
	@python3 eval/publish_dashboard.py \
	  --report eval/reports/synthetic_journeys-$(STAMP).json \
	  --report eval/reports/client_2026_11_23-$(STAMP).json \
	  --report eval/reports/client_uat_sheet-$(STAMP).json
	@python3 eval/synthetic_report.py \
	  --report eval/reports/synthetic_journeys-$(STAMP).json \
	  --report eval/reports/client_2026_11_23-$(STAMP).json \
	  --report eval/reports/client_uat_sheet-$(STAMP).json \
	  --out eval/reports/synthetic-summary-$(STAMP).txt

# Push the newest smoke result again. make smoke and make full already do this.
publish:
	@python3 eval/publish_dashboard.py
