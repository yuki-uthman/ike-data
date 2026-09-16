# Ike Data

The shared Odoo data pipeline for MRH Investment's dashboards. This repo owns
the Odoo credential and the scheduled pull; it has no frontend of its own.
Each dashboard ([ike-sales](https://github.com/yuki-uthman/ike-sales),
ike-expenses, [ike-today](https://github.com/yuki-uthman/ike-today)) is a
separate, static-only repo that fetches its JSON straight from here via
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
  today's sales, expenses *and* payments from Odoo in one job, commits all
  three JSON files together (kept as one workflow, not three, specifically to
  avoid independent crons racing each other's git push on the same repo).
- `scripts/odoo_payments.py` — **the shared "money received on a day" query.**
  Both dashboards now ask the same question, so it lives here once instead of
  being copied into three scripts and drifting. Sources `pos.payment` and
  posted inbound `account.payment`, drops any accounting payment already
  counted through a POS order, attaches each transaction's lines, and splits
  cash vs transfer by method/journal name with an `other` bucket for anything
  matching neither. Every optional field is probed with `fields_get` first, so
  an Odoo version lacking one degrades a column rather than failing the run.
- `scripts/fetch_sales.py` — today's entry for `sales.json`. **Changed meaning
  on 2026-09-16**: this counted sales *made* each day, paid or not; it now
  counts money *received*, matching ike-today. The day entry keeps its old key
  names, so no dashboard change was needed — `pos` and `regularSales` now mean
  "received through the POS" and "received through an accounting payment", and
  their sum is the day's takings.
- `scripts/fetch_expenses.py` — the expenses pull, sourced from `hr.expense`
  (this Odoo instance has no vendor bills at all - `hr.expense` is what's
  actually used to record day-to-day spend). Counts `approved` / `posted` /
  `in_payment` / `paid` as real; `draft` / `submitted` are tracked separately
  as pending and excluded from the total; `refused` is dropped entirely.
- `scripts/fetch_today.py` — the same day's payments written per-transaction
  into `today.json` for ike-today: invoice number, customer, amount, method,
  and the lines behind each one. A thin caller of `odoo_payments`; every run
  logs each distinct method and journal name it saw, so widening the
  cash/transfer patterns is a one-line change against real evidence.
- `scripts/backfill_sales.py` / `scripts/backfill_expenses.py` — one-off
  backfill of past days, run manually when needed. `backfill_sales.py` calls
  the same `odoo_payments.day_entry` the cron does, so a backfilled day and a
  live day can never be computed differently. Run it through the
  **Backfill sales history** workflow (Actions → Run workflow → number of
  days), since the Odoo credential only exists in Actions.
- `.github/workflows/backfill-sales.yml` — manual-only backfill. Shares the
  cron's concurrency group so the two can never rewrite `sales.json` at once.
- `data/sales.json`, `data/expenses.json`, `data/today.json` — the outputs.
  The first two keep a rolling 60-day history; `today.json` holds the current
  day only and is overwritten each run. Any dashboard can read any of them
  directly at, e.g.,
  `https://raw.githubusercontent.com/yuki-uthman/ike-data/main/data/sales.json`
  (GitHub serves raw file content with `Access-Control-Allow-Origin: *`, so
  this works from any origin, no CORS setup needed).

Adding another domain later (e.g. inventory) means one more `fetch_*.py`
script here, one more step in the same workflow, one more JSON file — same
repo, same secret, same retry logic, no new cron.

## One-time setup

1. **Add the Odoo API key as a repo secret**: Settings → Secrets and
   variables → Actions → New repository secret, name `ODOO_API_KEY`, value
   an Odoo API key scoped to **RPC**. (This is a *new* secret — GitHub does
   not let you copy a secret's value between repos, so it has to be
   re-entered here even if you already have one on another repo.)
2. Run the workflow once manually (Actions tab → "Refresh sales + expenses
   data" → Run workflow) to seed real data immediately.

## Reliability note

GitHub's own `schedule` trigger has repeatedly gone dormant for hours at a
time on this account (a known platform quirk, not a config issue — see the
commit history for the investigation). A [cron-job.org](https://cron-job.org)
job hitting this repo's `workflow_dispatch` API every 15 minutes is running
alongside it as the reliable trigger; GitHub's own schedule is left enabled
too, as a second, occasionally-redundant safety net. The workflow's commit
step retries with `git pull --rebase` on a push conflict, so having both
triggers fire close together is safe.
