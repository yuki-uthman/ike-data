# Ike Data

The shared Odoo data pipeline for MRH Investment's dashboards. This repo owns
the Odoo credential and the scheduled pull; it has no frontend of its own.
Each dashboard (e.g. [ike-sales](https://github.com/yuki-uthman/ike-sales))
is a separate, static-only repo that fetches its JSON straight from here via
`raw.githubusercontent.com` — no server, no API, no shared secret.

## Why split from the frontend

- **One place holds the Odoo API key** — new dashboards never need their own
  copy of the credential, they just read a public JSON file.
- **One place owns the push-retry logic** — every fetch script commits into
  the same repo/branch, so the race-condition handling only has to exist once.
- Adding a new dashboard is just "add a fetch script here, point a new static
  page at its output" — no new cron, no new secret.

## What's here

- `.github/workflows/refresh-sales.yml` — runs every 15 minutes, pulls
  today's sales from Odoo, commits `data/sales.json`.
- `scripts/fetch_sales.py` — the sales pull + de-duplication logic (see its
  own docstring).
- `scripts/backfill_sales.py` — one-off backfill of past days, run manually
  when needed.
- `data/sales.json` — the output. Any dashboard can read it directly at
  `https://raw.githubusercontent.com/yuki-uthman/ike-data/main/data/sales.json`
  (GitHub serves raw file content with `Access-Control-Allow-Origin: *`, so
  this works from any origin, no CORS setup needed).

A future expenses (or other) dashboard adds `scripts/fetch_expenses.py` +
its own workflow schedule here, publishing `data/expenses.json` alongside
`data/sales.json` — same repo, same secret, same retry logic.

## One-time setup

1. **Add the Odoo API key as a repo secret**: Settings → Secrets and
   variables → Actions → New repository secret, name `ODOO_API_KEY`, value
   an Odoo API key scoped to **RPC**. (This is a *new* secret — GitHub does
   not let you copy a secret's value between repos, so it has to be
   re-entered here even if you already have one on another repo.)
2. Run the workflow once manually (Actions tab → "Refresh sales data" → Run
   workflow) to seed real data immediately.

## Reliability note

GitHub's own `schedule` trigger has repeatedly gone dormant for hours at a
time on this account (a known platform quirk, not a config issue — see the
commit history for the investigation). A [cron-job.org](https://cron-job.org)
job hitting this repo's `workflow_dispatch` API every 15 minutes is running
alongside it as the reliable trigger; GitHub's own schedule is left enabled
too, as a second, occasionally-redundant safety net. The workflow's commit
step retries with `git pull --rebase` on a push conflict, so having both
triggers fire close together is safe.
