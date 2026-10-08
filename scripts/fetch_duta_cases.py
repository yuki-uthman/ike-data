#!/usr/bin/env python3
"""Pull how many CASES of CV Duta stock have been sold and write
data/duta-cases.json for the duta-cases dashboard.

THE UNIT IS THE CASE. Every CV Duta product exists three ways in Odoo - a
loose `pcs` piece (carries the stock), sometimes a `bundle`, and a `case`
kit (a phantom BoM over the pcs). This counts only the case product, in the
case UoM. A piece or a bundle sold on its own is not a case and is left out
on purpose; so is a case product sold in some other unit, which is reported
as a warning rather than guessed at.

WHAT "SOLD" MEANS
  + sale.order.line on orders in state sale/done (confirmed - not draft/sent)
  + pos.order.line rung at the till and paid, when no sale order sits behind it
  - a posted customer credit note that reverses a sale-order line
Draft and sent quotations are not sold; their cases are reported separately
as `quoted` so the page can say what is still pending without counting it.

Which products: every active-or-archived product of the vendor named
VENDOR_NAME (via product.supplierinfo) whose unit is `case`. A new Duta case
product appears on the page by itself once it has its vendor link.

Days are Maldives calendar days (UTC+5), the same clock as the other pipelines.
Reads ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY from the environment.
"""
import json
import os
import re
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

VENDOR_NAME = "CV Duta"
CASE_UOM_NAME = "case"
MALDIVES_OFFSET = timedelta(hours=5)
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "duta-cases.json"

SOLD_ORDER_STATES = ("sale", "done")
QUOTED_ORDER_STATES = ("draft", "sent")
SOLD_POS_STATES = ("paid", "done", "invoiced")


def clean_product(name):
    """'[1069260] Soklin Floor Cleaner Apple (Green / Hijau) - Case (12pcs, 400 ml)'
    -> ('Soklin Floor Cleaner Apple (Green)', '400 ml', '12 pcs')."""
    name = re.sub(r"^\[[^\]]*\]\s*", "", name).strip()
    m = re.search(r"\s*-\s*Case\s*\((.*)\)\s*$", name)
    base, inner = (name[: m.start()], m.group(1)) if m else (name, "")
    base = re.sub(r"\(([^)/]*?)\s*/\s*[^)]*\)", r"(\1)", base).strip()
    pack, size = "", ""
    parts = [p.strip() for p in inner.split(",")]
    if parts and parts[0]:
        pm = re.match(r"(\d+)\s*(pcs|bundles?)", parts[0], re.I)
        if pm:
            pack = f"{pm.group(1)} {pm.group(2).lower()}"
            size = ", ".join(parts[1:])
        else:
            size = ", ".join(parts)
    return base, size, pack


def maldives_day(utc_str):
    """Odoo's 'YYYY-MM-DD HH:MM:SS' (UTC) -> Maldives calendar date string."""
    dt = datetime.strptime(utc_str[:19], "%Y-%m-%d %H:%M:%S") + MALDIVES_OFFSET
    return dt.strftime("%Y-%m-%d")


def aggregate(products, sale_lines, quoted_lines, pos_lines, refund_lines):
    """Pure: turn already-fetched rows into the file's `days` / `quoted` / totals.

    Every *_lines row is a dict with `product_id`, `qty`, `order` (a unique
    key for counting orders) and, for sold rows, `date` (Maldives YYYY-MM-DD).
    refund_lines subtract. Returns (days, quoted, totals_by_product).
    """
    by_day = defaultdict(lambda: {"cases": 0.0, "orders": set(), "byProduct": defaultdict(float)})
    totals = defaultdict(float)
    for rows, sign in ((sale_lines, 1), (pos_lines, 1), (refund_lines, -1)):
        for r in rows:
            q = sign * r["qty"]
            d = by_day[r["date"]]
            d["cases"] += q
            d["byProduct"][str(r["product_id"])] += q
            if sign > 0:
                d["orders"].add(r["order"])
            totals[r["product_id"]] += q

    days = []
    for date in sorted(by_day):
        d = by_day[date]
        days.append({
            "date": date,
            "cases": round(d["cases"], 2),
            "orders": len(d["orders"]),
            "byProduct": {k: round(v, 2) for k, v in sorted(d["byProduct"].items(), key=lambda kv: int(kv[0])) if v},
        })

    quoted = {
        "cases": round(sum(r["qty"] for r in quoted_lines), 2),
        "orders": len({r["order"] for r in quoted_lines}),
    }
    return days, quoted, {pid: round(v, 2) for pid, v in totals.items()}


def collect(execute, now_utc):
    vendors = execute("res.partner", "search", [("name", "=", VENDOR_NAME)])
    if not vendors:
        raise SystemExit(f"No partner named {VENDOR_NAME!r} in Odoo")
    sup = execute("product.supplierinfo", "search_read", [("partner_id", "in", vendors)], fields=["product_tmpl_id"])
    tmpl_ids = sorted({s["product_tmpl_id"][0] for s in sup if s["product_tmpl_id"]})
    prods = execute(
        "product.product", "search_read",
        [("product_tmpl_id", "in", tmpl_ids), ("active", "in", [True, False])],
        fields=["name", "uom_id", "product_tmpl_id", "active"],
    )
    cases = [p for p in prods if p["uom_id"][1].lower() == CASE_UOM_NAME]
    if not cases:
        raise SystemExit("No case-unit products found for the vendor - nothing to count")
    ids = [p["id"] for p in cases]
    case_ids = set(ids)

    products = []
    for p in sorted(cases, key=lambda p: clean_product(p["name"])):
        base, size, pack = clean_product(p["name"])
        products.append({"id": p["id"], "name": base, "size": size, "pack": pack, "active": p["active"]})

    def is_case(uom_field):
        return bool(uom_field) and uom_field[1].lower() == CASE_UOM_NAME

    warnings = []

    # --- sale orders ---------------------------------------------------
    sol = execute(
        "sale.order.line", "search_read",
        [("product_id", "in", ids), ("order_id.state", "in", list(SOLD_ORDER_STATES + QUOTED_ORDER_STATES))],
        fields=["product_id", "product_uom_qty", "product_uom_id", "order_id"],
    )
    order_ids = sorted({r["order_id"][0] for r in sol})
    orders = {o["id"]: o for o in execute("sale.order", "read", order_ids, fields=["name", "date_order", "state"])} if order_ids else {}
    sale_lines, quoted_lines = [], []
    for r in sol:
        o = orders[r["order_id"][0]]
        if not is_case(r["product_uom_id"]):
            warnings.append(f"{o['name']}: case product sold in unit {r['product_uom_id'][1]!r} - not counted")
            continue
        row = {"product_id": r["product_id"][0], "qty": r["product_uom_qty"], "order": o["name"]}
        if o["state"] in SOLD_ORDER_STATES:
            sale_lines.append({**row, "date": maldives_day(o["date_order"])})
        else:
            quoted_lines.append(row)

    # --- the till ------------------------------------------------------
    # A POS line that settles a sale-order line is the same case as that
    # order's line, so it must not count twice: skip any with sale_order_line_id.
    pos_fields = execute("pos.order.line", "fields_get", attributes=["type"])
    want = ["product_id", "qty", "order_id", "product_uom_id"] if "product_uom_id" in pos_fields else ["product_id", "qty", "order_id"]
    if "sale_order_line_id" in pos_fields:
        want.append("sale_order_line_id")
    pol = execute("pos.order.line", "search_read", [("product_id", "in", ids)], fields=want)
    pos_orders = {}
    po_ids = sorted({r["order_id"][0] for r in pol})
    if po_ids:
        pos_orders = {o["id"]: o for o in execute("pos.order", "read", po_ids, fields=["name", "date_order", "state"])}
    pos_lines = []
    for r in pol:
        o = pos_orders[r["order_id"][0]]
        if o["state"] not in SOLD_POS_STATES or r.get("sale_order_line_id"):
            continue
        if "product_uom_id" in r and not is_case(r["product_uom_id"]):
            warnings.append(f"{o['name']}: case product rung in unit {r['product_uom_id'][1]!r} - not counted")
            continue
        pos_lines.append({"product_id": r["product_id"][0], "qty": r["qty"], "order": o["name"], "date": maldives_day(o["date_order"])})

    # --- credit notes against sale-order lines -------------------------
    # POS refunds already come through as negative POS lines above, so only a
    # credit note that traces back to a sale-order line is subtracted here.
    aml = execute(
        "account.move.line", "search_read",
        [("product_id", "in", ids), ("move_id.move_type", "=", "out_refund"),
         ("move_id.state", "=", "posted"), ("sale_line_ids", "!=", False)],
        fields=["product_id", "quantity", "product_uom_id", "move_id", "date"],
    )
    refund_lines = []
    for r in aml:
        if not is_case(r["product_uom_id"]):
            warnings.append(f"{r['move_id'][1]}: credit note in unit {r['product_uom_id'][1]!r} - not counted")
            continue
        refund_lines.append({"product_id": r["product_id"][0], "qty": r["quantity"], "order": r["move_id"][1], "date": str(r["date"])})

    days, quoted, totals = aggregate(products, sale_lines, quoted_lines, pos_lines, refund_lines)
    for p in products:
        p["cases"] = totals.get(p["id"], 0)
    return products, days, quoted, warnings


def main():
    url = os.environ["ODOO_URL"]
    db = os.environ["ODOO_DB"]
    username = os.environ["ODOO_USERNAME"]
    api_key = os.environ["ODOO_API_KEY"]

    uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, username, api_key, {})
    if not uid:
        raise SystemExit("Odoo authentication failed (UID: False) - check ODOO_API_KEY scope (must be RPC)")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

    def execute(model, method, *args, **kwargs):
        return models.execute_kw(db, uid, api_key, model, method, list(args), kwargs)

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    products, days, quoted, warnings = collect(execute, now_utc)

    payload = {
        "company": "MRH Investment",
        "supplier": VENDOR_NAME,
        "unit": "case",
        "generatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totalCases": round(sum(p["cases"] for p in products), 2),
        "quoted": quoted,
        "products": products,
        "days": days,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    print(
        f"Wrote {DATA_PATH}: {payload['totalCases']} cases sold across {len(days)} day(s), "
        f"{sum(1 for p in products if p['cases'])}/{len(products)} products moved; "
        f"{quoted['cases']} more cases quoted in {quoted['orders']} unconfirmed order(s)"
    )
    for w in warnings:
        print(f"::warning::{w}")


if __name__ == "__main__":
    main()
