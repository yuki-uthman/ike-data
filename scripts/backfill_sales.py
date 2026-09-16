#!/usr/bin/env python3
"""Backfill past days into data/sales.json using the same logic as fetch_sales.py.

Usage:
    python scripts/backfill_sales.py 2026-09-06 2026-09-07 ... 2026-09-12
    python scripts/backfill_sales.py --days 7   # the 7 Maldives calendar days before today
    python scripts/backfill_sales.py --days 60  # rewrite the whole retained history

"Same logic" is literal: both scripts call odoo_payments.day_entry, so a
backfilled day and a live day can never be computed differently. That
matters more than usual right now - sales.json changed meaning from "sold
that day" to "received that day", and any day not rewritten still holds the
old number, which the chart would show side by side with new ones as though
they were comparable.

Never touches today's date - that stays owned by the live 15-minute cron
(scripts/fetch_sales.py). Reads ODOO_URL / ODOO_DB / ODOO_USERNAME /
ODOO_API_KEY from the environment, same as fetch_sales.py.
"""
import json
import os
import sys
import xmlrpc.client
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import odoo_payments as op

HISTORY_CAP = 60
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sales.json"


def main():
    args = sys.argv[1:]
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    today = op.maldives_today(now_utc)

    if args and args[0] == "--days":
        n = int(args[1])
        dates = [today - timedelta(days=i) for i in range(n, 0, -1)]
    elif args:
        dates = [date.fromisoformat(a) for a in args]
    else:
        raise SystemExit("Usage: backfill_sales.py <YYYY-MM-DD> [...] | --days N")

    skipped_today = [d for d in dates if d >= today]
    dates = [d for d in dates if d < today]
    if skipped_today:
        print(f"Skipping {', '.join(d.isoformat() for d in skipped_today)} - today belongs to fetch_sales.py")
    if not dates:
        raise SystemExit("Nothing to backfill")

    url = os.environ["ODOO_URL"]
    db = os.environ["ODOO_DB"]
    username = os.environ["ODOO_USERNAME"]
    api_key = os.environ["ODOO_API_KEY"]

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, api_key, {})
    if not uid:
        raise SystemExit("Odoo authentication failed (UID: False) - check ODOO_API_KEY scope (must be RPC)")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def execute(model, method, *a, **kw):
        return models.execute_kw(db, uid, api_key, model, method, list(a), kw)

    payload = json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() else {"company": "MRH Investment", "currency": "MVR", "days": []}
    by_date = {d["date"]: d for d in payload.get("days", [])}

    generated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for d in dates:
        entry, _transactions, skipped_as_pos = op.day_entry(execute, d, generated_at)
        received = round(entry["pos"]["total"] + entry["regularSales"]["total"], 2)
        was = by_date.get(entry["date"])
        before = round(was["pos"]["total"] + was["regularSales"]["total"], 2) if was else None
        by_date[entry["date"]] = entry
        change = f" (was {before})" if before is not None and before != received else ""
        print(
            f"  {entry['date']}: Received {received}{change} - "
            f"Cash {entry['cash']['total']}, Transfer {entry['transfer']['total']}, "
            f"Other {entry['other']['total']}, Products {len(entry['products'])}, "
            f"{skipped_as_pos} skipped as already-counted POS"
        )

    days = sorted(by_date.values(), key=lambda d: d["date"])
    payload["days"] = days[-HISTORY_CAP:]

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {DATA_PATH} with {len(payload['days'])} total day(s)")


if __name__ == "__main__":
    main()
