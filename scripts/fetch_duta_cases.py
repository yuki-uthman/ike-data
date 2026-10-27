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

PROFIT %: per product, (net price - landed cost) / net price, where cost is the
case product's standard_price and net price is ex-GST (Odoo prices include
GST, cost does not). For a product that has sold, net price is what was
actually charged (confirmed sale-order lines, subtotal / cases); for one that
has not, it is the list price less GST, flagged with basis "list".
A product at or below cost reads 0%, not a negative number.

SHIPMENT PROFIT: `shipments` lists every container, oldest first. For each
product on it: `cases` ordered, `cost` and `netPrice` per case (ex-GST),
`profitPerCase` = netPrice - cost and `profit` = that times `cases` - what the
container earns if every case sells at the same net price the % above uses.
Unlike the %, the money figures are NOT floored, so a product sold below cost
shows a negative profit and the total stays honest. Cost and price are published
by the owner's decision; the repo is public.
  - A shipment from Odoo is one confirmed purchase order (state purchase/done)
    of the vendor; its cost is the product's landed standard_price.
  - A PROFORMA shipment comes from shipments/next-container.json, kept by hand
    until the order exists in Odoo (delete the file's entry then, or the cases
    count twice). Same prices, freight and markup as before are assumed, so its
    cost is the product's Odoo standard_price - except a line carrying a
    `usdPrice` (a price that changed), costed as usdPrice x mvrPerUsd x landedFactor.

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
ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "duta-cases.json"
PROFORMA_PATH = ROOT / "shipments" / "next-container.json"

SOLD_ORDER_STATES = ("sale", "done")
QUOTED_ORDER_STATES = ("draft", "sent")
SOLD_POS_STATES = ("paid", "done", "invoiced")
SHIPPED_ORDER_STATES = ("purchase", "done")


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


def margin_pct(cost, net_price):
    """Profit as a % of the net selling price, 1 dp; None if either is unknown.

    Floored at 0: a product sold at or below cost reads 0%, by decision of the
    owner, so the published file never carries a loss figure."""
    if not cost or cost <= 0 or not net_price or net_price <= 0:
        return None
    return max(0.0, round((net_price - cost) / net_price * 100, 1))


def unit_profit(cost, net_price):
    """Money profit on one case (net price - cost), 2 dp; None if either is unknown.

    Not floored: see SHIPMENT PROFIT in the module docstring."""
    if not cost or cost <= 0 or not net_price or net_price <= 0:
        return None
    return round(net_price - cost, 2)


def shipment_lines(rows, net_by_id):
    """Pure: [(product_id, cases, cost)] -> published lines with the money profit.

    `net_by_id` is the per-case net (ex-GST) price used for each product."""
    lines = []
    for pid, cases, cost in rows:
        net = net_by_id.get(pid)
        per = unit_profit(cost, net)
        lines.append({
            "productId": pid, "cases": round(cases, 2), "cost": round(cost, 2),
            "netPrice": round(net, 2) if net else None,
            "profitPerCase": per, "profit": None if per is None else round(per * cases, 2),
        })
    return sorted(lines, key=lambda l: l["productId"])


def proforma_rows(spec, odoo_cost):
    """Pure: the input file's dict -> [(product_id, cases, landed cost per case)].

    A line without `usdPrice` costs what Odoo says today (`odoo_cost[product_id]`);
    one with it is priced from the invoice: USD x mvrPerUsd x landedFactor."""
    k = spec["mvrPerUsd"] * spec["landedFactor"]
    return [(l["productId"], l["cases"],
             round(l["usdPrice"] * k, 2) if "usdPrice" in l else odoo_cost[l["productId"]])
            for l in spec["lines"]]


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


def load_proforma(path, by_id, net_by_id, warnings):
    """The hand-kept proforma shipment, as a list of 0 or 1 shipments."""
    if not path.exists():
        return []
    spec = json.loads(path.read_text())
    unknown = [l["productId"] for l in spec["lines"] if l["productId"] not in by_id]
    if unknown:
        warnings.append(f"{path.name}: product ids {unknown} are not CV Duta case products - skipped")
    spec = {**spec, "lines": [l for l in spec["lines"] if l["productId"] in by_id]}
    return [{
        "ref": spec["ref"], "date": spec["date"], "status": "proforma",
        "lines": shipment_lines(proforma_rows(spec, {pid: p["_cost"] for pid, p in by_id.items()}), net_by_id),
    }]


def collect(execute, now_utc):
    vendors = execute("res.partner", "search", [("name", "=", VENDOR_NAME)])
    if not vendors:
        raise SystemExit(f"No partner named {VENDOR_NAME!r} in Odoo")
    sup = execute("product.supplierinfo", "search_read", [("partner_id", "in", vendors)], fields=["product_tmpl_id"])
    tmpl_ids = sorted({s["product_tmpl_id"][0] for s in sup if s["product_tmpl_id"]})
    prods = execute(
        "product.product", "search_read",
        [("product_tmpl_id", "in", tmpl_ids), ("active", "in", [True, False])],
        fields=["name", "uom_id", "product_tmpl_id", "active", "standard_price", "lst_price", "taxes_id"],
    )
    tax_ids = sorted({t for p in prods for t in p["taxes_id"]})
    taxes = {t["id"]: t for t in execute("account.tax", "read", tax_ids, fields=["amount", "amount_type", "price_include"])} if tax_ids else {}

    def gst_pct(p):
        """Percent tax baked into this product's price (0 if none is included)."""
        return sum(taxes[t]["amount"] for t in p["taxes_id"]
                   if t in taxes and taxes[t]["price_include"] and taxes[t]["amount_type"] == "percent")
    cases = [p for p in prods if p["uom_id"][1].lower() == CASE_UOM_NAME]
    if not cases:
        raise SystemExit("No case-unit products found for the vendor - nothing to count")
    ids = [p["id"] for p in cases]
    case_ids = set(ids)

    products = []
    for p in sorted(cases, key=lambda p: clean_product(p["name"])):
        base, size, pack = clean_product(p["name"])
        products.append({"id": p["id"], "name": base, "size": size, "pack": pack, "active": p["active"]})
        p["_cost"], p["_list_net"] = p["standard_price"], p["lst_price"] / (1 + gst_pct(p) / 100)

    def is_case(uom_field):
        return bool(uom_field) and uom_field[1].lower() == CASE_UOM_NAME

    warnings = []

    # --- sale orders ---------------------------------------------------
    sol = execute(
        "sale.order.line", "search_read",
        [("product_id", "in", ids), ("order_id.state", "in", list(SOLD_ORDER_STATES + QUOTED_ORDER_STATES))],
        fields=["product_id", "product_uom_qty", "product_uom_id", "order_id", "price_subtotal"],
    )
    order_ids = sorted({r["order_id"][0] for r in sol})
    orders = {o["id"]: o for o in execute("sale.order", "read", order_ids, fields=["name", "date_order", "state"])} if order_ids else {}
    sale_lines, quoted_lines = [], []
    net_rev, net_qty = defaultdict(float), defaultdict(float)  # per product, confirmed sale lines
    for r in sol:
        o = orders[r["order_id"][0]]
        if not is_case(r["product_uom_id"]):
            warnings.append(f"{o['name']}: case product sold in unit {r['product_uom_id'][1]!r} - not counted")
            continue
        row = {"product_id": r["product_id"][0], "qty": r["product_uom_qty"], "order": o["name"]}
        if o["state"] in SOLD_ORDER_STATES:
            sale_lines.append({**row, "date": maldives_day(o["date_order"])})
            net_rev[row["product_id"]] += r["price_subtotal"]
            net_qty[row["product_id"]] += r["product_uom_qty"]
        else:
            quoted_lines.append(row)

    # --- the shipments -------------------------------------------------
    # One per confirmed purchase order of the vendor, in the case unit.
    pol_ship = execute(
        "purchase.order.line", "search_read",
        [("product_id", "in", ids), ("order_id.state", "in", list(SHIPPED_ORDER_STATES))],
        fields=["product_id", "product_qty", "uom_id", "order_id"],
    )
    po_ids = sorted({r["order_id"][0] for r in pol_ship})
    po_info = {o["id"]: o for o in execute("purchase.order", "read", po_ids, fields=["name", "date_order", "receipt_status"])} if po_ids else {}
    ship_qty = defaultdict(lambda: defaultdict(float))  # purchase order id -> product -> cases
    for r in pol_ship:
        if not is_case(r["uom_id"]):
            warnings.append(f"{r['order_id'][1]}: case product bought in unit {r['uom_id'][1]!r} - not counted")
            continue
        ship_qty[r["order_id"][0]][r["product_id"][0]] += r["product_qty"]

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
    by_id = {p["id"]: p for p in cases}
    net_by_id = {}
    for p in products:
        p["cases"] = totals.get(p["id"], 0)
        src = by_id[p["id"]]
        if net_qty.get(p["id"]):
            net, p["profitBasis"] = net_rev[p["id"]] / net_qty[p["id"]], "sold"
        else:
            net, p["profitBasis"] = src["_list_net"], "list"
        p["profitPct"] = margin_pct(src["_cost"], net)
        net_by_id[p["id"]] = net

    shipments = []
    for po_id in sorted(ship_qty, key=lambda i: (po_info[i]["date_order"], i)):
        o = po_info[po_id]
        rows = [(pid, q, by_id[pid]["_cost"]) for pid, q in ship_qty[po_id].items() if q]
        shipments.append({
            "ref": o["name"], "date": maldives_day(o["date_order"]),
            "status": "received" if o.get("receipt_status") == "full" else "ordered",
            "lines": shipment_lines(rows, net_by_id),
        })
    shipments.extend(load_proforma(PROFORMA_PATH, by_id, net_by_id, warnings))
    return products, days, quoted, shipments, warnings


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
    products, days, quoted, shipments, warnings = collect(execute, now_utc)

    payload = {
        "company": "MRH Investment",
        "supplier": VENDOR_NAME,
        "unit": "case",
        "currency": "MVR",
        "generatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totalCases": round(sum(p["cases"] for p in products), 2),
        "quoted": quoted,
        "products": products,
        "shipments": shipments,
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
