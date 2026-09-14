#!/usr/bin/env python3
"""Pull today's expenses from Odoo and update data/expenses.json.

Source: hr.expense (this Odoo instance has no vendor bills at all - this is
the model actually used to record day-to-day spend, e.g. "Shop Rent",
"Gate Pass (Boat Delivery)").

Counted as a real, confirmed expense: state in (approved, posted,
in_payment, paid) - i.e. at least manager-approved, not just entered.
"draft"/"submitted" are tracked separately as pending (mirrors how
unconfirmed sale-order quotations are handled) and excluded from the
total. "refused" expenses are dropped entirely - they never happened.

Runs on the same GitHub Actions schedule as fetch_sales.py; reads
ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY from the environment.
"""
import json
import os
import xmlrpc.client
from datetime import datetime, timedelta, timezone
from pathlib import Path

MALDIVES_OFFSET = timedelta(hours=5)
HISTORY_CAP = 60
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "expenses.json"

CONFIRMED_STATES = ["approved", "posted", "in_payment", "paid"]
PENDING_STATES = ["draft", "submitted"]


def maldives_today_utc(now_utc):
    return (now_utc + MALDIVES_OFFSET).date()


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
    date_str = maldives_today_utc(now_utc).isoformat()

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

    day_entry = {
        "date": date_str,
        "generatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confirmed": {"total": round(confirmed_total, 2), "count": len(confirmed)},
        "pending": {"total": round(pending_total, 2), "count": len(pending)},
        "categories": categories,
    }

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
    print(f"Wrote {DATA_PATH}: {date_str} -> Confirmed {confirmed_total} ({len(confirmed)}), "
          f"Pending {pending_total} ({len(pending)}), Categories {len(categories)}")


if __name__ == "__main__":
    main()
