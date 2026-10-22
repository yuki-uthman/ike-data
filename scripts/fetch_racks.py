#!/usr/bin/env python3
"""Count racks sold per month and write data/racks.json for the ike-racks page.

Two sources, joined per rack row and per month:
  zoho  history up to 26 Aug 2026, from paid Zoho invoices. Static: it lives in
        scripts/racks_history.json (row definitions + the monthly Zoho counts)
        and is never re-derived, because Zoho is no longer in use.
  odoo  live, from 27 Aug 2026 (ODOO_SINCE) to now. Recomputed on every run.

THE UNIT IS THE RACK. A product sold as a set counts one rack per bay: the
code `2C-WHT-DS-2000-X03` is a 3-bay double side, so a line of qty 2 is 6 racks.

WHAT "SOLD" MEANS (Odoo)
  + sale.order.line on orders in state sale/done (confirmed, not draft/sent/cancel)
  + pos.order.line on paid POS orders, when no sale order sits behind the line
Invoices are NOT a third source. Every invoice of these products traces back to
a sale order (`invoice_origin`), so counting it would count the sale twice. A
credit note is not subtracted either: the only one so far reverses a line whose
sale-order quantity was already set to 0, so subtracting would count it twice.
A POS refund is a negative POS line and does subtract.

LEFT OUT ON PURPOSE
  - orders whose customer is named "Test", and the orders in EXCLUDED_ORDERS
    (duplicates the owner ruled on by hand, 2026-10-11)
  - round-shelf end racks (the "End Round Shelves" products and the old legacy
    "2C ER Round W") and add-on bays: not confirmed as racks
  - a line with no product (free text)

Heights: a coded product states its height. A legacy-named product (old
Zoho-era records, mostly archived) does not, so LEGACY below gives a default
(2C single side 2000mm, 2C double side and end rack 1800mm, RD8 1950mm), and a
height written in the order line's own text wins over it ("6.5ft" -> 2000mm).

ONE SALE ENTERED TWICE: a till line that repeats a sale-order line - same
customer, product, quantity and price, within an hour - is one sale, not two.
It happens when a quotation is paid at the till without being linked to it. The
till line is dropped and logged.

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

MALDIVES_OFFSET = timedelta(hours=5)
ODOO_SINCE = "2026-08-27"  # first Maldives day counted from Odoo; Zoho covers the days before
HISTORY_PATH = Path(__file__).resolve().parent / "racks_history.json"
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "racks.json"

SOLD_ORDER_STATES = ("sale", "done")
SOLD_POS_STATES = ("paid", "done", "invoiced")

# Orders ruled on by hand when the Odoo half was first built (odoo/rack_sales_2C_RD8.xlsx,
# sheet "Duplicates & Flags"). Test orders are caught by customer name; these are not.
EXCLUDED_ORDERS = {
    "S00140": "same order as S00188, which is the corrected one and was paid",
    "S00154": "rung up again at the till as POS Ike - 000101, which was paid",
}
TEST_CUSTOMERS = {"test"}

RACK_CODE = re.compile(r"^(2C|RD8)-(BLK|WHT|GRY)-(SS|DS|ER)[PF]?-(\d{4})(?:-X(\d+))?$")
SHELF_CODE = re.compile(r"^(2C|RD8)-(BLK|WHT|GRY)-SHTOP-(\d+)(?:x\d+)?$")
COLOUR = {"BLK": "Black", "WHT": "White", "GRY": "Gray"}
TYPE = {"SS": "Single Side", "DS": "Double Side", "ER": "End Rack"}

# Legacy-named products: Odoo product id -> (series, colour, type, height mm, bays).
LEGACY = {
    1595: ("2C", "Black", "Double Side", 1800, 2), 1597: ("2C", "Black", "Double Side", 1800, 3),
    1740: ("2C", "Black", "End Rack", 1800, 1), 1568: ("2C", "Black", "Single Side", 2000, 1),
    1439: ("2C", "White", "Double Side", 1800, 1), 1652: ("2C", "White", "Double Side", 1800, 1),
    1458: ("2C", "White", "Double Side", 1800, 2), 1467: ("2C", "White", "Double Side", 1800, 2),
    1653: ("2C", "White", "Double Side", 1800, 2), 1491: ("2C", "White", "Double Side", 1800, 3),
    1450: ("2C", "White", "End Rack", 1800, 1), 1347: ("2C", "White", "Single Side", 2000, 1),
    1447: ("2C", "White", "Single Side", 2000, 1), 1461: ("2C", "White", "Single Side", 2000, 2),
    1457: ("2C", "White", "Single Side", 2000, 6),
    1312: ("RD8", "Gray", "Double Side", 1950, 1), 1544: ("RD8", "Gray", "Double Side", 1950, 1),
    1620: ("RD8", "Gray", "Double Side", 1950, 2), 1357: ("RD8", "Gray", "End Rack", 1950, 1),
    1321: ("RD8", "White", "Double Side", 1950, 1), 1365: ("RD8", "White", "End Rack", 1950, 1),
    1329: ("RD8", "White", "Single Side", 1950, 1), 1521: ("RD8", "White", "Single Side", 1950, 7),
}

PRICE_BORDER_2C = re.compile(r"^2C Price Border Strip (Red|White|Green|Black)$")
PRICE_BORDER_RD8 = re.compile(r"^RD8 Price Border Strip\s+(White|Red|Green|Black|Blue) 900mm")
STEEL_BARRIER = re.compile(r"^2C (White|Black) Steel Barrier 900mm$")
SIMPLE_ROWS = {  # product code -> row name, for the one-product-one-row items
    "RACKLKHM": "Lui Kaana Haru Medium 5 Layers",
    "RACKLKHS": "Lui Kaana Haru Small 4 Layer",
    "70161": "Vegetable Rack 4 Square Baskets",
    "70186": "Trolley Stand 5 Layer Black",
    "70187": "Trolley Stand 5 Layer White",
}
# A product whose name starts with "#" is a composite rack kit. One of those that
# sells but maps to no row is worth a warning; parts and other items are not.
KIT_NAME = re.compile(r"^#(2C|RD8)\b")


def maldives_day(utc_str):
    """Odoo's 'YYYY-MM-DD HH:MM:SS' (UTC) -> Maldives calendar date string."""
    dt = datetime.strptime(utc_str[:19], "%Y-%m-%d %H:%M:%S") + MALDIVES_OFFSET
    return dt.strftime("%Y-%m-%d")


LEGACY_HEIGHT_TEXT = {  # series -> [(pattern on the order-line text, height mm)], first match wins
    "2C": [(re.compile(r"6\.5\s*ft", re.I), 2000), (re.compile(r"(?<![\d.])6\s*ft", re.I), 1800)],
    "RD8": [(re.compile(r"6\.5\s*ft", re.I), 1950), (re.compile(r"(?<![\d.])5\s*ft", re.I), 1550)],
}
DUPLICATE_WINDOW = timedelta(hours=1)


def legacy_row(pid, text):
    """A legacy product's (row name, bays); the height in the line's text, if any, beats the default."""
    series, col, typ, height, bays = LEGACY[pid]
    for pat, h in LEGACY_HEIGHT_TEXT[series]:
        if pat.search(text or ""):
            height = h
            break
    return f"{series} {col} {typ} {height}mm", bays


def classify(pid, code, name):
    """One Odoo product -> (row name, racks per unit), or None if it is not a counted row."""
    code, name = (code or "").strip(), (name or "").strip()
    m = RACK_CODE.match(code)
    if m:
        series, col, typ, height, bays = m.groups()
        return f"{series} {COLOUR[col]} {TYPE[typ]} {height}mm", int(bays or 1)
    if pid in LEGACY:
        series, col, typ, height, bays = LEGACY[pid]
        return f"{series} {col} {typ} {height}mm", bays
    m = SHELF_CODE.match(code)
    if m:
        series, col, width = m.groups()
        return f"{series} {COLOUR[col]} top shelf {width}mm", 1
    if code in SIMPLE_ROWS:
        return SIMPLE_ROWS[code], 1
    m = PRICE_BORDER_2C.match(name)
    if m:
        return f"2C Price border · {m.group(1)}", 1
    m = PRICE_BORDER_RD8.match(name)
    if m:
        return f"RD8 Price border · {m.group(1)}", 1
    m = STEEL_BARRIER.match(name)
    if m:
        return f"2C Steel barrier 900mm · {m.group(1)}", 1
    return None


def parse_utc(utc_str):
    return datetime.strptime(utc_str[:19], "%Y-%m-%d %H:%M:%S")


def sale_key(partner_id, product_id, qty, price):
    return (partner_id, product_id, qty, price)


def is_repeat(seen, partner_id, product_id, qty, price, when):
    """True when `seen` (sale-order lines keyed by sale_key -> [order times]) already holds
    this same customer / product / quantity / price within DUPLICATE_WINDOW of `when`.
    Only a positive quantity can repeat a sale; a till refund never does."""
    if qty <= 0:
        return False
    return any(abs(when - t) <= DUPLICATE_WINDOW for t in seen.get(sale_key(partner_id, product_id, qty, price), ()))


def month_list(first, last):
    """['2025-10', ..., '2026-10'] inclusive, from 'YYYY-MM' to 'YYYY-MM'."""
    y, mo = int(first[:4]), int(first[5:7])
    out = []
    while f"{y:04d}-{mo:02d}" <= last:
        out.append(f"{y:04d}-{mo:02d}")
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return out


def aggregate(lines):
    """Pure: [{row, racks, date}] -> {row name: {'YYYY-MM': racks}}."""
    out = defaultdict(lambda: defaultdict(float))
    for r in lines:
        out[r["row"]][r["date"][:7]] += r["racks"]
    return out


def build_payload(history, odoo_by_row, now_utc):
    """Pure: the static history + live Odoo counts -> the file's JSON, plus warnings.

    The month axis runs from the history's first month to the current Maldives
    month, so the file grows by one column each month with no change anywhere."""
    now_month = (now_utc + MALDIVES_OFFSET).strftime("%Y-%m")
    months = month_list(history["months"][0], max(now_month, history["months"][-1]))
    pad = len(months) - len(history["months"])
    warnings = []
    known = {p["name"] for p in history["products"]}
    for name in sorted(set(odoo_by_row) - known):
        warnings.append(f"sold from Odoo but has no row on the page, not shown: {name}")
    products = []
    for p in history["products"]:
        got = odoo_by_row.get(p["name"], {})
        odoo = [int(round(got.get(m, 0))) for m in months]
        products.append({**{k: v for k, v in p.items() if k != "zoho"}, "zoho": p["zoho"] + [0] * pad, "odoo": odoo})
    return {
        "company": "MRH Investment",
        "generatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "odooSince": ODOO_SINCE,
        "months": months,
        "products": products,
    }, warnings


def collect(execute, now_utc):
    """Ask Odoo for every counted line since ODOO_SINCE. Returns (lines, warnings)."""
    since_utc = (datetime.strptime(ODOO_SINCE, "%Y-%m-%d") - MALDIVES_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
    pats = ["2C", "RD8", "Lui Kaana", "Vegetable Rack 4 Square", "Trolley Stand 5", "Price Border", "Steel Barrier"]
    dom = ["|"] * (len(pats)) + [("id", "in", sorted(LEGACY))] + [("name", "ilike", p) for p in pats]
    prods = execute("product.product", "search_read", dom, fields=["name", "default_code"], context={"active_test": False})
    row_of, unmapped = {}, []
    for p in prods:
        c = classify(p["id"], p["default_code"], p["name"])
        if c:
            row_of[p["id"]] = c
        elif KIT_NAME.match(p["name"]):
            unmapped.append(p)
    ids = sorted(row_of)
    warnings = []

    def is_test(partner):
        return bool(partner) and partner[1].strip().lower() in TEST_CUSTOMERS

    def row_for(pid, text):
        return legacy_row(pid, text) if pid in LEGACY else row_of[pid]

    # --- sale orders ---------------------------------------------------
    sol = execute(
        "sale.order.line", "search_read",
        [("product_id", "in", ids), ("order_id.state", "in", list(SOLD_ORDER_STATES)), ("order_id.date_order", ">=", since_utc)],
        fields=["product_id", "product_uom_qty", "price_unit", "name", "order_id"],
    )
    order_ids = sorted({r["order_id"][0] for r in sol})
    orders = {o["id"]: o for o in execute("sale.order", "read", order_ids, fields=["name", "date_order", "partner_id"])} if order_ids else {}
    lines, skipped, seen = [], defaultdict(int), defaultdict(list)
    for r in sol:
        o = orders[r["order_id"][0]]
        if o["name"] in EXCLUDED_ORDERS or is_test(o["partner_id"]):
            skipped[o["name"]] += 1
            continue
        row, per = row_for(r["product_id"][0], r["name"])
        lines.append({"row": row, "racks": r["product_uom_qty"] * per, "date": maldives_day(o["date_order"]), "ref": o["name"]})
        if o["partner_id"]:
            seen[sale_key(o["partner_id"][0], r["product_id"][0], r["product_uom_qty"], r["price_unit"])].append(parse_utc(o["date_order"]))

    # --- the till ------------------------------------------------------
    # A POS line that settles a sale-order line is the same rack as that order's
    # line, so skip any with sale_order_line_id.
    pos_fields = execute("pos.order.line", "fields_get", attributes=["type"])
    want = ["product_id", "qty", "price_unit", "full_product_name", "order_id"] + (["sale_order_line_id"] if "sale_order_line_id" in pos_fields else [])
    pol = execute("pos.order.line", "search_read", [("product_id", "in", ids), ("order_id.date_order", ">=", since_utc)], fields=want)
    po_ids = sorted({r["order_id"][0] for r in pol})
    pos_orders = {o["id"]: o for o in execute("pos.order", "read", po_ids, fields=["name", "date_order", "state", "partner_id"])} if po_ids else {}
    for r in pol:
        o = pos_orders[r["order_id"][0]]
        if o["state"] not in SOLD_POS_STATES or r.get("sale_order_line_id"):
            continue
        if is_test(o["partner_id"]):
            skipped[o["name"]] += 1
            continue
        if o["partner_id"] and is_repeat(seen, o["partner_id"][0], r["product_id"][0], r["qty"], r["price_unit"], parse_utc(o["date_order"])):
            skipped[o["name"]] += 1
            continue
        row, per = row_for(r["product_id"][0], r.get("full_product_name"))
        lines.append({"row": row, "racks": r["qty"] * per, "date": maldives_day(o["date_order"]), "ref": o["name"]})

    if skipped:
        print("Held out: " + ", ".join(f"{k} ({v} line{'s' if v > 1 else ''})" for k, v in sorted(skipped.items())))
    # An unmapped rack kit that has actually sold would silently vanish from the page.
    if unmapped:
        um_ids = [p["id"] for p in unmapped]
        sold = execute("sale.order.line", "search_read",
                       [("product_id", "in", um_ids), ("order_id.state", "in", list(SOLD_ORDER_STATES)), ("order_id.date_order", ">=", since_utc)],
                       fields=["product_id"])
        for pid in sorted({r["product_id"][0] for r in sold}):
            warnings.append(f"rack kit sold but not mapped to a row: {next(p['name'] for p in unmapped if p['id'] == pid)}")
    return lines, warnings


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
    history = json.loads(HISTORY_PATH.read_text())
    lines, warnings = collect(execute, now_utc)
    payload, more = build_payload(history, aggregate(lines), now_utc)
    warnings += more

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=1) + "\n")

    racks = sum(sum(p["odoo"]) for p in payload["products"] if p["series"] in ("2C", "RD8") and p["type"] != "Price border")
    print(f"Wrote {DATA_PATH}: {len(lines)} Odoo line(s) since {ODOO_SINCE} across {len(payload['products'])} rows, "
          f"{len(payload['months'])} months (rows incl. parts and borders; {racks} 2C/RD8 racks from Odoo)")
    for w in warnings:
        print(f"::warning::{w}")


if __name__ == "__main__":
    main()
