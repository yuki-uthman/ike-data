#!/usr/bin/env python3
"""Backfill past days into data/expenses.json using the same logic as fetch_expenses.py.

Usage:
    python scripts/backfill_expenses.py 2026-09-06 2026-09-07 ... 2026-09-13
    python scripts/backfill_expenses.py --days 7   # the 7 Maldives calendar days before today

Never touches today's date - that stays owned by the live cron
(scripts/fetch_expenses.py). Reads ODOO_URL / ODOO_DB / ODOO_USERNAME /
ODOO_API_KEY from the environment, same as fetch_expenses.py.
"""
import json
import os
import sys
import xmlrpc.client
from datetime import date, datetime, timedelta
from pathlib import Path

MALDIVES_OFFSET = timedelta(hours=5)
HISTORY_CAP = 60
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "expenses.json"

CONFIRMED_STATES = ["approved", "posted", "in_payment", "paid"]
PENDING_STATES = ["draft", "submitted"]


def maldives_today():
    return (datetime.utcnow() + MALDIVES_OFFSET).date()


def fetch_day(execute, d):
    date_str = d.isoformat()

    confirmed = execute(
        "hr.expense", "search_read",
        [["date", "=", date_str], ["state", "in", CONFIRMED_STATES]],
        fields=["total_amount", "product_id"],
    )
    confirmed_total = sum(e["total_amount"] for e in confirmed)

    pending = execute(
        "hr.expense", "search_read",
        [["date", "=", date_str], ["state", "in", PENDING_STATES]],
        fields=["total_amount"],
    )
    pending_total = sum(e["total_amount"] for e in pending)

    category_totals = {}
    for e in confirmed:
        if not e["product_id"]:
            continue
        pid, pname = e["product_id"]
        c = category_totals.setdefault(pid, {"name": pname, "count": 0, "total": 0.0})
        c["count"] += 1
        c["total"] += e["total_amount"]

    categories = sorted(
        ({"name": c["name"], "count": c["count"], "total": round(c["total"], 2)} for c in category_totals.values()),
        key=lambda c: c["total"], reverse=True,
    )

    return {
        "date": date_str,
        "generatedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confirmed": {"total": round(confirmed_total, 2), "count": len(confirmed)},
        "pending": {"total": round(pending_total, 2), "count": len(pending)},
        "categories": categories,
    }


def main():
    args = sys.argv[1:]
    today = maldives_today()

    if args and args[0] == "--days":
        n = int(args[1])
        dates = [today - timedelta(days=i) for i in range(n, 0, -1)]
    elif args:
        dates = [date.fromisoformat(a) for a in args]
    else:
        raise SystemExit("Usage: backfill_expenses.py <YYYY-MM-DD> [...] | --days N")

    dates = [d for d in dates if d != today]
    if not dates:
        raise SystemExit("Nothing to backfill (all requested dates were today, which the live cron owns).")

    url = os.environ["ODOO_URL"]
    db = os.environ["ODOO_DB"]
    username = os.environ["ODOO_USERNAME"]
    api_key = os.environ["ODOO_API_KEY"]

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, api_key, {})
    if not uid:
        raise SystemExit("Odoo authentication failed (UID: False)")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def execute(model, method, *a, **kw):
        return models.execute_kw(db, uid, api_key, model, method, list(a), kw)

    payload = json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() else {"company": "MRH Investment", "currency": "MVR", "days": []}
    by_date = {d["date"]: d for d in payload.get("days", [])}

    for d in sorted(dates):
        entry = fetch_day(execute, d)
        by_date[d.isoformat()] = entry
        print(f"{entry['date']} -> Confirmed {entry['confirmed']['total']} ({entry['confirmed']['count']}), "
              f"Pending {entry['pending']['total']} ({entry['pending']['count']}), "
              f"Categories {len(entry['categories'])}")

    days = sorted(by_date.values(), key=lambda d: d["date"])
    payload["days"] = days[-HISTORY_CAP:]
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {DATA_PATH} with {len(payload['days'])} total day(s)")


if __name__ == "__main__":
    main()
