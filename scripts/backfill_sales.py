#!/usr/bin/env python3
"""Backfill past days into data/sales.json using the same logic as fetch_sales.py.

Usage:
    python scripts/backfill_sales.py 2026-09-06 2026-09-07 ... 2026-09-12
    python scripts/backfill_sales.py --days 7   # the 7 Maldives calendar days before today

Never touches today's date - that stays owned by the live 15-minute cron
(scripts/fetch_sales.py). Reads ODOO_URL / ODOO_DB / ODOO_USERNAME /
ODOO_API_KEY from the environment, same as fetch_sales.py.
"""
import json
import os
import sys
import xmlrpc.client
from datetime import date, datetime, timedelta
from pathlib import Path

MALDIVES_OFFSET = timedelta(hours=5)
HISTORY_CAP = 60
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sales.json"


def maldives_today():
    return (datetime.utcnow() + MALDIVES_OFFSET).date()


def day_bounds_utc(d):
    start_utc = datetime(d.year, d.month, d.day) - MALDIVES_OFFSET
    return start_utc, start_utc + timedelta(days=1)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fetch_day(execute, d):
    start_utc, end_utc = day_bounds_utc(d)
    domain_window = [["date_order", ">=", fmt(start_utc)], ["date_order", "<", fmt(end_utc)]]

    pos_orders = execute("pos.order", "search_read", domain_window + [["state", "in", ["paid", "done"]]], fields=["id", "amount_total"])
    pos_total = sum(o["amount_total"] for o in pos_orders)
    pos_order_ids = [o["id"] for o in pos_orders]

    pos_linked_sale_order_ids = set()
    if pos_order_ids:
        lines = execute("pos.order.line", "search_read", [["order_id", "in", pos_order_ids], ["sale_order_origin_id", "!=", False]], fields=["sale_order_origin_id"])
        for line in lines:
            pos_linked_sale_order_ids.add(line["sale_order_origin_id"][0])

    confirmed_orders = execute("sale.order", "search_read", domain_window + [["state", "=", "sale"]], fields=["id", "amount_total"])
    regular_orders = [o for o in confirmed_orders if o["id"] not in pos_linked_sale_order_ids]
    regular_total = sum(o["amount_total"] for o in regular_orders)

    pending_orders = execute("sale.order", "search_read", domain_window + [["state", "in", ["draft", "sent"]]], fields=["id", "amount_total"])
    pending_total = sum(o["amount_total"] for o in pending_orders)

    product_totals = {}

    def add_line(product_field, qty, amount):
        if not product_field:
            return
        product_id, product_name = product_field
        entry = product_totals.setdefault(product_id, {"name": product_name, "qty": 0.0, "total": 0.0})
        entry["qty"] += qty
        entry["total"] += amount

    if pos_order_ids:
        pos_lines = execute("pos.order.line", "search_read", [["order_id", "in", pos_order_ids]], fields=["product_id", "qty", "price_subtotal_incl"])
        for line in pos_lines:
            add_line(line["product_id"], line["qty"], line["price_subtotal_incl"])

    regular_order_ids = [o["id"] for o in regular_orders]
    if regular_order_ids:
        so_lines = execute("sale.order.line", "search_read", [["order_id", "in", regular_order_ids], ["display_type", "=", False]], fields=["product_id", "product_uom_qty", "price_total"])
        for line in so_lines:
            add_line(line["product_id"], line["product_uom_qty"], line["price_total"])

    products = sorted(
        ({"name": p["name"], "qty": round(p["qty"], 2), "total": round(p["total"], 2)} for p in product_totals.values()),
        key=lambda p: p["total"], reverse=True,
    )

    return {
        "date": d.isoformat(),
        "generatedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pos": {"total": round(pos_total, 2), "count": len(pos_orders)},
        "regularSales": {"total": round(regular_total, 2), "count": len(regular_orders)},
        "pendingQuotations": {"total": round(pending_total, 2), "count": len(pending_orders)},
        "products": products,
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
        raise SystemExit("Usage: backfill_sales.py <YYYY-MM-DD> [...] | --days N")

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
        print(f"{entry['date']} -> POS {entry['pos']['total']} ({entry['pos']['count']}), "
              f"Regular {entry['regularSales']['total']} ({entry['regularSales']['count']}), "
              f"Products {len(entry['products'])}")

    days = sorted(by_date.values(), key=lambda d: d["date"])
    payload["days"] = days[-HISTORY_CAP:]
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {DATA_PATH} with {len(payload['days'])} total day(s)")


if __name__ == "__main__":
    main()
