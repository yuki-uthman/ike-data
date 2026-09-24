#!/usr/bin/env python3
"""Write today's customer payments to data/today.json for ike-today.

All of the Odoo work lives in odoo_payments.collect_payments - see that
module for what counts as money received, how POS and accounting payments
are de-duplicated, and why the cash/transfer split is name-driven and
self-reporting. This script only decides which day to ask for and what
shape to write.

data/today.json holds one day and is overwritten each run; ike-sales keeps
the 60-day history of the same underlying question.

Runs on a GitHub Actions schedule; reads ODOO_URL / ODOO_DB /
ODOO_USERNAME / ODOO_API_KEY from the environment.
"""
import json
import os
import xmlrpc.client
from datetime import datetime, timezone
from pathlib import Path

import odoo_payments as op

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "today.json"


def write_today(transactions, skipped, day, now_utc, cash_outs=None, till=None):
    """Write data/today.json from an already-collected day.

    Split out so fetch_sales.py, which collects the very same day's payments,
    can write this file too instead of asking Odoo the same question twice.
    """
    cash_outs = cash_outs or []
    payload = {
        "company": "MRH Investment",
        "currency": "MVR",
        "date": day.isoformat(),
        "generatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cash": op.bucket(transactions, "cash"),
        "transfer": op.bucket(transactions, "transfer"),
        "other": op.bucket(transactions, "other"),
        "transactions": transactions,
        "methodsSeen": op.methods_seen(transactions),
        "cashOut": op.bucket_out(cash_outs),
        "cashOutTransactions": cash_outs,
        "till": till,
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    print(
        f"Wrote {DATA_PATH}: {payload['date']} -> "
        f"Cash {payload['cash']['total']} ({payload['cash']['count']}), "
        f"Transfer {payload['transfer']['total']} ({payload['transfer']['count']}), "
        f"Other {payload['other']['total']} ({payload['other']['count']}), "
        f"Cash Out {payload['cashOut']['total']} ({payload['cashOut']['count']}), "
        f"skipped {skipped['pos_invoice']} already-counted POS invoice(s), "
        f"{skipped['pos_settlement']} POS session settlement(s)"
    )
    if payload["till"]:
        t = payload["till"]
        print(
            f"Till: opening {t['opening']}, expected closing {t['expectedClosing']}, "
            f"counted {t['counted']}, difference {t['difference']} "
            f"({'closed' if t['closed'] else 'still open'}, {t['sessionCount']} session(s))"
        )
    else:
        print("Till: no POS session today")
    with_lines = sum(1 for t in transactions if t["lines"])
    print(f"{with_lines}/{len(transactions)} transaction(s) carry line detail")
    print("Payment method / journal names seen today:")
    for entry in payload["methodsSeen"]:
        print(f"  {entry['name']!r} -> {entry['method']} ({entry['count']} txn, {entry['total']})")
    if not payload["methodsSeen"]:
        print("  (none - no customer payments recorded yet today)")
    if payload["other"]["count"]:
        print("::warning::Unclassified payment method(s) present - widen CASH_PATTERN / TRANSFER_PATTERN in odoo_payments.py")


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

    transactions, skipped = op.collect_payments(execute, day)
    cash_outs = op.collect_cash_outs(execute, day)
    till = op.collect_till(execute, day)
    write_today(transactions, skipped, day, now_utc, cash_outs, till)


if __name__ == "__main__":
    main()
