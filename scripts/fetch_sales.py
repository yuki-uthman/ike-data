#!/usr/bin/env python3
"""Pull today's sales from Odoo and update data/sales.json.

Counts each transaction exactly once:
  - Point of Sale: every POS order (paid/done) dated "today" in Maldives time.
  - Sales Orders: confirmed sale orders dated "today" whose lines were NOT
    settled through the POS register (sale_order_origin_id on pos.order.line
    is how Odoo links a sale order to the POS order that paid it).
  - Pending Quotations: draft/sent (unconfirmed) sale orders, shown separately
    and excluded from the total.
  - Products: every order line behind the above, aggregated by product and
    ranked by sales value - same de-duplication rule (a sale order settled
    through POS contributes via its POS lines only, never both).

Runs on a GitHub Actions schedule; reads ODOO_URL / ODOO_DB / ODOO_USERNAME /
ODOO_API_KEY from the environment.
"""
import json
import os
import xmlrpc.client
from datetime import datetime, timedelta, timezone
from pathlib import Path

MALDIVES_OFFSET = timedelta(hours=5)
HISTORY_CAP = 60
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "sales.json"


def maldives_day_bounds_utc(now_utc):
    maldives_now = now_utc + MALDIVES_OFFSET
    maldives_date = maldives_now.date()
    start_utc = datetime(maldives_date.year, maldives_date.month, maldives_date.day) - MALDIVES_OFFSET
    end_utc = start_utc + timedelta(days=1)
    return maldives_date, start_utc, end_utc


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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
    maldives_date, start_utc, end_utc = maldives_day_bounds_utc(now_utc)
    date_str = maldives_date.isoformat()
    domain_window = [["date_order", ">=", fmt(start_utc)], ["date_order", "<", fmt(end_utc)]]

    # 1. POS orders today (paid or done = completed, money collected)
    pos_orders = execute(
        "pos.order", "search_read",
        domain_window + [["state", "in", ["paid", "done"]]],
        fields=["id", "amount_total"],
    )
    pos_total = sum(o["amount_total"] for o in pos_orders)
    pos_count = len(pos_orders)
    pos_order_ids = [o["id"] for o in pos_orders]

    # 2. Which sale orders are already represented inside those POS orders?
    #    (a sale order whose lines were settled through the POS register)
    pos_linked_sale_order_ids = set()
    if pos_order_ids:
        lines = execute(
            "pos.order.line", "search_read",
            [["order_id", "in", pos_order_ids], ["sale_order_origin_id", "!=", False]],
            fields=["sale_order_origin_id"],
        )
        for line in lines:
            pos_linked_sale_order_ids.add(line["sale_order_origin_id"][0])

    # 3. Confirmed sale orders today, excluding ones already counted via POS
    confirmed_orders = execute(
        "sale.order", "search_read",
        domain_window + [["state", "=", "sale"]],
        fields=["id", "amount_total"],
    )
    regular_orders = [o for o in confirmed_orders if o["id"] not in pos_linked_sale_order_ids]
    regular_total = sum(o["amount_total"] for o in regular_orders)
    regular_count = len(regular_orders)

    # 4. Pending (unconfirmed) quotations today - informational only
    pending_orders = execute(
        "sale.order", "search_read",
        domain_window + [["state", "in", ["draft", "sent"]]],
        fields=["id", "amount_total"],
    )
    pending_total = sum(o["amount_total"] for o in pending_orders)
    pending_count = len(pending_orders)

    # 5. Per-product breakdown, same de-duplication as the total: POS lines
    #    (all of them) + regular sale order lines (the ones NOT already
    #    represented via POS).
    product_totals = {}

    def add_line(product_field, qty, amount):
        if not product_field:
            return
        product_id, product_name = product_field
        entry = product_totals.setdefault(product_id, {"name": product_name, "qty": 0.0, "total": 0.0})
        entry["qty"] += qty
        entry["total"] += amount

    if pos_order_ids:
        pos_lines = execute(
            "pos.order.line", "search_read",
            [["order_id", "in", pos_order_ids]],
            fields=["product_id", "qty", "price_subtotal_incl"],
        )
        for line in pos_lines:
            add_line(line["product_id"], line["qty"], line["price_subtotal_incl"])

    regular_order_ids = [o["id"] for o in regular_orders]
    if regular_order_ids:
        so_lines = execute(
            "sale.order.line", "search_read",
            [["order_id", "in", regular_order_ids], ["display_type", "=", False]],
            fields=["product_id", "product_uom_qty", "price_total"],
        )
        for line in so_lines:
            add_line(line["product_id"], line["product_uom_qty"], line["price_total"])

    products = sorted(
        (
            {"name": p["name"], "qty": round(p["qty"], 2), "total": round(p["total"], 2)}
            for p in product_totals.values()
        ),
        key=lambda p: p["total"],
        reverse=True,
    )

    day_entry = {
        "date": date_str,
        "generatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pos": {"total": round(pos_total, 2), "count": pos_count},
        "regularSales": {"total": round(regular_total, 2), "count": regular_count},
        "pendingQuotations": {"total": round(pending_total, 2), "count": pending_count},
        "products": products,
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
    print(f"Wrote {DATA_PATH}: {date_str} -> POS {pos_total} ({pos_count}), Regular {regular_total} ({regular_count}), Pending {pending_total} ({pending_count}), Products {len(products)}")


if __name__ == "__main__":
    main()
