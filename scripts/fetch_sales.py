#!/usr/bin/env python3
"""Pull today's money received from Odoo and update data/sales.json.

CHANGED MEANING: this used to count sales MADE today - every confirmed sale
order and POS order dated today, paid or not. It now counts money RECEIVED
today, the same question ike-today asks, so the two dashboards can never
disagree. A credit sale confirmed today no longer appears here until the
payment actually lands, and an old invoice settled today appears here today.

All of the Odoo work lives in odoo_payments - see that module for what
counts, how POS and accounting payments are de-duplicated, and the partial
payment caveat on the product breakdown.

The day entry keeps its existing key names so the dashboard needs no
matching change: `pos` and `regularSales` now mean "received through the
POS" and "received through an accounting payment", and the page's
pos + regularSales sum is the day's money in. `cash` / `transfer` / `other`
are carried alongside them.

Runs on a GitHub Actions schedule; reads ODOO_URL / ODOO_DB /
ODOO_USERNAME / ODOO_API_KEY from the environment.
"""
import json
import os
import xmlrpc.client
from datetime import datetime, timezone
from pathlib import Path

import fetch_today
import odoo_payments as op

HISTORY_CAP = 60
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sales.json"


def main():
    url = os.environ["ODOO_URL"]
    db = os.environ["ODOO_DB"]
    username = os.environ["ODOO_USERNAME"]
    api_key = os.environ["ODOO_API_KEY"]

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, api_key, {})
    if not uid:
        raise SystemExit("Odoo authentication failed (UID: False) - check ODOO_API_KEY scope (must be RPC)")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def execute(model, method, *args, **kwargs):
        return models.execute_kw(db, uid, api_key, model, method, list(args), kwargs)

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    day = op.maldives_today(now_utc)
    generated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    day_entry, transactions, skipped = op.day_entry(execute, day, generated_at)
    date_str = day_entry["date"]

    # Same day, same payments: write today.json from this collection rather
    # than running a second identical pass over Odoo in a separate step.
    fetch_today.write_today(transactions, skipped, day, now_utc, day_entry["cashOutTransactions"])

    if DATA_PATH.exists():
        payload = json.loads(DATA_PATH.read_text())
    else:
        payload = {"company": "MRH Investment", "currency": "MVR", "days": []}

    days = [d for d in payload.get("days", []) if d["date"] != date_str]
    days.append(day_entry)
    days.sort(key=lambda d: d["date"])
    payload["days"] = days[-HISTORY_CAP:]

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    received = round(day_entry["pos"]["total"] + day_entry["regularSales"]["total"], 2)
    print(
        f"Wrote {DATA_PATH}: {date_str} -> Received {received} "
        f"(POS {day_entry['pos']['total']} / {day_entry['pos']['count']}, "
        f"Accounting {day_entry['regularSales']['total']} / {day_entry['regularSales']['count']}), "
        f"Cash {day_entry['cash']['total']}, Transfer {day_entry['transfer']['total']}, "
        f"Other {day_entry['other']['total']}, Transactions {len(day_entry['transactions'])}, "
        f"Cash Out {day_entry['cashOut']['total']} / {day_entry['cashOut']['count']}, "
        f"skipped {skipped['pos_invoice']} already-counted POS invoice(s), "
        f"{skipped['pos_settlement']} POS session settlement(s)"
    )
    if day_entry["other"]["count"]:
        print("::warning::Unclassified payment method(s) present - widen CASH_PATTERN / TRANSFER_PATTERN in odoo_payments.py")


if __name__ == "__main__":
    main()
