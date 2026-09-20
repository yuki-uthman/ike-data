#!/usr/bin/env python3
"""Pull every still-open quotation and unpaid order into data/quotations.json.

This is the follow-up question, not the money question the other scripts ask:
what has NOT closed yet. A record belongs here while any part of its value is
unsettled, and leaves the moment the last rufiyaa arrives - whichever way it
arrived.

WHY THE OBVIOUS FIELD IS NOT USED. `sale.order.invoice_ids` is empty on most
settled orders in this instance, and `invoice_status` lies in the other
direction: S00203 reads "Fully Invoiced" with zero linked invoices. Trusting
either would leave dozens of already-paid orders sitting on the follow-up
list forever. Money actually reaches an order two ways here, and each needs
its own join:

  - account.move.invoice_origin : the ORDER NAME written on the invoice.
    Carried by 154 of 155 customer invoices; `invoice_ids` is not.
  - pos.order.line.sale_order_line_id : the counter settling a quotation
    against its order lines directly, producing no sale-order invoice at all.
    625 POS lines point back this way.

So "paid" is computed, never read:

    outstanding = amount_total - (paid invoice value + POS-settled value)

and a record is open while that is above one cent. Partial payments fall out
of this for free - the row shows the remaining balance, not the order total.

WHAT IS DELIBERATELY EXCLUDED
  - Cancelled orders (state 'cancel') - closed, not pending.
  - Reversed invoices - a credit note leaves residual 0, which would
    otherwise read as "paid" when the sale was in fact undone.
  - Draft and cancelled POS orders - not money.
  - Section and note lines (display_type set) - not products.

Dismissing a dead lead is NOT represented here. That is the dashboard's own
browser-local state; this file always reports what Odoo actually holds.

Runs on a GitHub Actions schedule; reads ODOO_URL / ODOO_DB /
ODOO_USERNAME / ODOO_API_KEY from the environment.
"""
import json
import os
import re
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import odoo_payments as op

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "quotations.json"

# A cent of float noise must not keep a fully settled order on the list.
EPSILON = 0.01

QUOTATION_STATES = ("draft", "sent")
POS_NOT_MONEY = ("draft", "cancel")


def split_origins(origin):
    """invoice_origin is free text and may carry several order names."""
    if not origin:
        return []
    return [tok for tok in re.split(r"[,\s]+", str(origin)) if tok]


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
    today = op.maldives_today(now_utc)

    orders = execute(
        "sale.order", "search_read", [["state", "!=", "cancel"]],
        fields=["name", "state", "amount_total", "date_order", "validity_date",
                "partner_id", "user_id", "invoice_ids", "order_line"],
    )

    # --- Channel 1: accounting invoices, reached by id AND by origin name ---
    moves = execute(
        "account.move", "search_read",
        [["move_type", "in", ["out_invoice", "out_receipt"]], ["state", "=", "posted"]],
        fields=["move_type", "payment_state", "amount_total", "amount_residual", "invoice_origin"],
    )
    move_by_id = {m["id"]: m for m in moves}
    moves_by_origin = defaultdict(list)
    for m in moves:
        for token in split_origins(m["invoice_origin"]):
            moves_by_origin[token].append(m)

    # --- Channel 2: the POS counter, settling order lines directly ---
    pos_lines = execute(
        "pos.order.line", "search_read", [["sale_order_line_id", "!=", False]],
        fields=["order_id", "price_subtotal_incl", "sale_order_line_id"],
    )
    # Only the POS orders those lines point at - not every till receipt ever
    # rung up, which is by far the largest table this script touches.
    pos_order_ids = sorted({op.rel_id(pl["order_id"]) for pl in pos_lines} - {None})
    pos_orders = {
        p["id"]: p
        for p in (execute("pos.order", "search_read", [["id", "in", pos_order_ids]], fields=["state"])
                  if pos_order_ids else [])
    }
    line_to_order = {lid: o["id"] for o in orders for lid in o["order_line"]}
    pos_settled = defaultdict(float)
    for pl in pos_lines:
        pos_order = pos_orders.get(op.rel_id(pl["order_id"]))
        if not pos_order or pos_order["state"] in POS_NOT_MONEY:
            continue
        order_id = line_to_order.get(op.rel_id(pl["sale_order_line_id"]))
        if order_id:
            pos_settled[order_id] += pl["price_subtotal_incl"]

    def invoice_settled(order):
        seen, total = set(), 0.0
        candidates = [move_by_id[i] for i in order["invoice_ids"] if i in move_by_id]
        candidates += moves_by_origin.get(order["name"], [])
        for m in candidates:
            if m["id"] in seen or m["payment_state"] == "reversed":
                continue
            seen.add(m["id"])
            total += m["amount_total"] - m["amount_residual"]
        return total

    open_orders = []
    for o in orders:
        settled = invoice_settled(o) + pos_settled.get(o["id"], 0.0)
        outstanding = round(o["amount_total"] - settled, 2)
        if outstanding > EPSILON:
            open_orders.append((o, round(settled, 2), outstanding))

    # --- Lines, for the chevron detail. Only for orders that made the cut. ---
    line_ids = [lid for o, _, _ in open_orders for lid in o["order_line"]]
    raw_lines = execute(
        "sale.order.line", "read", line_ids,
        fields=["order_id", "name", "product_uom_qty", "price_total", "display_type"],
    ) if line_ids else []
    lines_by_order = defaultdict(list)
    for l in raw_lines:
        if l.get("display_type"):
            continue
        lines_by_order[op.rel_id(l["order_id"])].append(
            {"name": l["name"], "qty": l["product_uom_qty"], "total": round(l["price_total"], 2)}
        )

    quotations = []
    for o, settled, outstanding in open_orders:
        day = o["date_order"][:10] if o["date_order"] else None
        quotations.append({
            "id": o["id"],
            "ref": o["name"],
            "kind": "quotation" if o["state"] in QUOTATION_STATES else "order",
            "customer": op.rel_name(o["partner_id"]),
            "salesperson": op.rel_name(o["user_id"]),
            "date": day,
            "expires": o["validity_date"] or None,
            "total": round(o["amount_total"], 2),
            "settled": settled,
            "outstanding": outstanding,
            "lines": lines_by_order.get(o["id"], []),
        })
    # Newest first, biggest first within a day - the page re-sorts per view.
    quotations.sort(key=lambda q: (q["date"] or "", q["outstanding"]), reverse=True)

    # --- Day totals for the chart. Gaps filled so the x-axis is a real
    # timeline, not a list of days that happen to have records. The span runs
    # from the oldest still-open record to today, so it shrinks as old
    # quotations are settled or cancelled rather than growing forever. ---
    per_day = defaultdict(lambda: {"count": 0, "outstanding": 0.0, "total": 0.0})
    for q in quotations:
        if not q["date"]:
            continue
        bucket = per_day[q["date"]]
        bucket["count"] += 1
        bucket["outstanding"] += q["outstanding"]
        bucket["total"] += q["total"]

    days = []
    if per_day:
        cursor = datetime.strptime(min(per_day), "%Y-%m-%d").date()
        while cursor <= today:
            key = cursor.isoformat()
            bucket = per_day.get(key, {"count": 0, "outstanding": 0.0, "total": 0.0})
            days.append({
                "date": key,
                "count": bucket["count"],
                "outstanding": round(bucket["outstanding"], 2),
                "total": round(bucket["total"], 2),
            })
            cursor += timedelta(days=1)

    payload = {
        "company": "MRH Investment",
        "currency": "MVR",
        "generatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "openCount": len(quotations),
        "openTotal": round(sum(q["outstanding"] for q in quotations), 2),
        "quotationCount": sum(1 for q in quotations if q["kind"] == "quotation"),
        "quotationTotal": round(sum(q["outstanding"] for q in quotations if q["kind"] == "quotation"), 2),
        "orderCount": sum(1 for q in quotations if q["kind"] == "order"),
        "orderTotal": round(sum(q["outstanding"] for q in quotations if q["kind"] == "order"), 2),
        "days": days,
        "quotations": quotations,
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    print(
        f"Wrote {DATA_PATH}: {payload['openCount']} open "
        f"({payload['quotationCount']} quotation / {payload['orderCount']} unpaid order), "
        f"{payload['openTotal']} outstanding across {len(days)} day(s)"
    )
    print(f"  scanned {len(orders)} live order(s), {len(moves)} posted customer invoice(s), "
          f"{len(pos_lines)} POS line(s) linked to an order line")
    settled_via_pos = sum(1 for o, s, _ in open_orders if pos_settled.get(o["id"], 0.0) > EPSILON)
    part_paid = [q for q in quotations if q["settled"] > EPSILON]
    print(f"  {len(part_paid)} part-paid record(s) still open; {settled_via_pos} of them part-settled at the POS")
    missing_lines = [q["ref"] for q in quotations if not q["lines"]]
    if missing_lines:
        print(f"::warning::{len(missing_lines)} open record(s) carry no product lines "
              f"- their row will not expand: {', '.join(missing_lines[:10])}")


if __name__ == "__main__":
    main()
