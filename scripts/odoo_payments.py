#!/usr/bin/env python3
"""Shared "money received on a given day" query, used by every script here.

Both dashboards now ask the same question - what actually arrived, not what
was sold - so the query lives once, here, instead of being copied into
fetch_today.py, fetch_sales.py and backfill_sales.py and drifting apart.
Those three differ only in which day(s) they ask for and what they write.

Money reaches this business two ways, so a day is the union of:
  - pos.payment      : every payment line on a POS order, timestamped in the
                       Maldives day window.
  - account.payment  : posted inbound customer payments booked on that date
                       (bank receipts, counter receipts against invoices).

De-duplication: a POS order that was also invoiced can produce an
account.payment reconciled against that same invoice. Any account.payment
whose reconciled invoices are ALL invoices already reached through a POS
order that day is dropped, so such a sale is counted once, via its POS line.

Each transaction carries the lines behind it - product, quantity and
tax-inclusive line total - taken from pos.order.line or from the invoice(s)
the payment reconciles. Note the consequence for a PARTIAL payment: the
lines describe the whole invoice, so they total more than the payment that
paid for them. MRH settles invoices in full as a rule, so this is a stated
edge rather than a routine distortion.

Classification is name-driven and deliberately self-reporting: the payment
method (POS) or journal (accounting) name decides the bucket, the raw name
travels with every row, and anything matching neither pattern lands in
"other" - visible, counted in no total - rather than silently inflating
"transfer".

Every optional field is probed with fields_get before it is requested, so an
Odoo version that lacks one degrades that column to a fallback instead of
failing the whole run.
"""
import re
from datetime import datetime, timedelta

MALDIVES_OFFSET = timedelta(hours=5)

CASH_PATTERN = re.compile(r"cash", re.IGNORECASE)
TRANSFER_PATTERN = re.compile(r"transfer|bank|bml|mib|mfl|online|deposit", re.IGNORECASE)

# Odoo has renamed these across versions (posted/sent/reconciled -> in_process/paid),
# so exclude what is definitely not money rather than listing what is.
NOT_RECEIVED_STATES = ["draft", "cancel", "canceled", "cancelled"]


def maldives_today(now_utc):
    return (now_utc + MALDIVES_OFFSET).date()


def day_bounds_utc(day):
    """The UTC window covering one Maldives (UTC+5) calendar day."""
    start_utc = datetime(day.year, day.month, day.day) - MALDIVES_OFFSET
    return start_utc, start_utc + timedelta(days=1)


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def classify(name):
    """Bucket a payment method / journal name. Cash wins over transfer: a
    journal called "Cash Deposit" is cash, not a transfer."""
    if not name:
        return "other"
    if CASH_PATTERN.search(name):
        return "cash"
    if TRANSFER_PATTERN.search(name):
        return "transfer"
    return "other"


def rel_name(value):
    """Odoo returns a many2one as [id, display_name], or False when unset."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[1]
    return None


def rel_id(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0]
    return None


def local_time(dt_string):
    """Odoo datetimes come back as naive UTC strings."""
    if not dt_string:
        return None
    try:
        stamp = datetime.strptime(dt_string[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (stamp + MALDIVES_OFFSET).strftime("%H:%M")


def collect_payments(execute, day):
    """Every customer payment received on one Maldives day.

    `execute(model, method, *args, **kwargs)` is the caller's bound XML-RPC
    helper. Returns (transactions, skipped_as_pos), transactions sorted by
    amount descending, each shaped:

        {ref, customer, amount, method, rawMethod, time, source, lines[]}
    """
    start_utc, end_utc = day_bounds_utc(day)
    date_str = day.isoformat()

    def existing_fields(model, wanted):
        available = set(execute(model, "fields_get", [], attributes=["type"]).keys())
        return [f for f in wanted if f in available]

    transactions = []
    pos_invoice_ids = set()

    # ---- 1. POS payments ----------------------------------------------------
    pos_payment_fields = existing_fields(
        "pos.payment", ["amount", "payment_date", "payment_method_id", "pos_order_id"]
    )
    pos_payments = execute(
        "pos.payment", "search_read",
        [["payment_date", ">=", fmt_dt(start_utc)], ["payment_date", "<", fmt_dt(end_utc)]],
        fields=pos_payment_fields,
    )

    pos_order_ids = sorted({rel_id(p.get("pos_order_id")) for p in pos_payments} - {None})
    pos_orders = {}
    pos_lines_by_order = {}
    if pos_order_ids:
        order_fields = existing_fields("pos.order", ["name", "partner_id", "account_move", "state"])
        for order in execute("pos.order", "search_read", [["id", "in", pos_order_ids]], fields=order_fields):
            pos_orders[order["id"]] = order
            invoice_id = rel_id(order.get("account_move"))
            if invoice_id:
                pos_invoice_ids.add(invoice_id)

        pos_line_fields = existing_fields(
            "pos.order.line", ["order_id", "product_id", "qty", "price_subtotal_incl"]
        )
        for line in execute(
            "pos.order.line", "search_read", [["order_id", "in", pos_order_ids]], fields=pos_line_fields
        ):
            name = rel_name(line.get("product_id"))
            if not name:
                continue
            pos_lines_by_order.setdefault(rel_id(line.get("order_id")), []).append({
                "name": name,
                "qty": round(line.get("qty") or 0.0, 2),
                "total": round(line.get("price_subtotal_incl") or 0.0, 2),
            })

    for payment in pos_payments:
        order = pos_orders.get(rel_id(payment.get("pos_order_id")), {})
        # A refund is a negative payment line - it belongs in the day's net cash.
        amount = payment.get("amount") or 0.0
        if not amount:
            continue
        # Invoice number when the POS order was invoiced, else the receipt reference.
        ref = rel_name(order.get("account_move")) or order.get("name") or "POS"
        method_name = rel_name(payment.get("payment_method_id"))
        transactions.append({
            "ref": ref,
            "customer": rel_name(order.get("partner_id")) or "Walk-in",
            "amount": round(amount, 2),
            "method": classify(method_name),
            "rawMethod": method_name or "unknown",
            "time": local_time(payment.get("payment_date")),
            "source": "pos",
            "lines": sorted(
                pos_lines_by_order.get(rel_id(payment.get("pos_order_id")), []),
                key=lambda l: l["total"], reverse=True,
            ),
        })

    # ---- 2. Accounting customer payments ------------------------------------
    payment_fields = existing_fields(
        "account.payment",
        ["amount", "date", "partner_id", "journal_id", "ref", "name", "reconciled_invoice_ids"],
    )
    account_payments = execute(
        "account.payment", "search_read",
        [
            ["date", "=", date_str],
            ["payment_type", "=", "inbound"],
            ["partner_type", "=", "customer"],
            ["state", "not in", NOT_RECEIVED_STATES],
        ],
        fields=payment_fields,
    )

    invoice_ids = sorted({
        move_id for payment in account_payments for move_id in (payment.get("reconciled_invoice_ids") or [])
    })
    invoice_names = {}
    invoice_lines_by_move = {}
    if invoice_ids:
        for move in execute("account.move", "search_read", [["id", "in", invoice_ids]], fields=["name"]):
            invoice_names[move["id"]] = move["name"]

        # product_id filters out the receivable, tax and section lines in one go.
        move_line_fields = existing_fields(
            "account.move.line", ["move_id", "product_id", "quantity", "price_total"]
        )
        for line in execute(
            "account.move.line", "search_read",
            [["move_id", "in", invoice_ids], ["product_id", "!=", False]],
            fields=move_line_fields,
        ):
            name = rel_name(line.get("product_id"))
            if not name:
                continue
            invoice_lines_by_move.setdefault(rel_id(line.get("move_id")), []).append({
                "name": name,
                "qty": round(line.get("quantity") or 0.0, 2),
                "total": round(line.get("price_total") or 0.0, 2),
            })

    skipped_as_pos = 0
    for payment in account_payments:
        amount = payment.get("amount") or 0.0
        if not amount:
            continue
        reconciled = payment.get("reconciled_invoice_ids") or []
        # Already counted through its POS line - drop it rather than double count.
        if reconciled and all(move_id in pos_invoice_ids for move_id in reconciled):
            skipped_as_pos += 1
            continue
        ref = next(
            (invoice_names[move_id] for move_id in reconciled if move_id in invoice_names),
            payment.get("ref") or payment.get("name") or "-",
        )
        journal_name = rel_name(payment.get("journal_id"))
        transactions.append({
            "ref": ref,
            "customer": rel_name(payment.get("partner_id")) or "Walk-in",
            "amount": round(amount, 2),
            "method": classify(journal_name),
            "rawMethod": journal_name or "unknown",
            "time": None,  # account.payment carries a date, not a time of day
            "source": "payment",
            # One payment can settle several invoices; show every line it paid for.
            "lines": sorted(
                [line for move_id in reconciled for line in invoice_lines_by_move.get(move_id, [])],
                key=lambda l: l["total"], reverse=True,
            ),
        })

    transactions.sort(key=lambda t: t["amount"], reverse=True)
    return transactions, skipped_as_pos


def bucket(transactions, kind):
    rows = [t for t in transactions if t["method"] == kind]
    return {"total": round(sum(t["amount"] for t in rows), 2), "count": len(rows)}


def by_source(transactions, source):
    rows = [t for t in transactions if t["source"] == source]
    return {"total": round(sum(t["amount"] for t in rows), 2), "count": len(rows)}


def aggregate_products(transactions):
    """Every product paid for that day, aggregated by name, ranked by value.

    Product names are kept exactly as Odoo holds them, internal reference
    prefix included; stripping that is a display concern and belongs in the
    dashboards, which already do it.
    """
    totals = {}
    for t in transactions:
        for line in t["lines"]:
            entry = totals.setdefault(line["name"], {"name": line["name"], "qty": 0.0, "total": 0.0})
            entry["qty"] += line["qty"]
            entry["total"] += line["total"]
    return sorted(
        ({"name": p["name"], "qty": round(p["qty"], 2), "total": round(p["total"], 2)} for p in totals.values()),
        key=lambda p: p["total"], reverse=True,
    )


def methods_seen(transactions):
    seen = {}
    for t in transactions:
        entry = seen.setdefault(t["rawMethod"], {"name": t["rawMethod"], "method": t["method"], "count": 0, "total": 0.0})
        entry["count"] += 1
        entry["total"] += t["amount"]
    for entry in seen.values():
        entry["total"] = round(entry["total"], 2)
    return sorted(seen.values(), key=lambda m: m["total"], reverse=True)


def pending_quotations(execute, day):
    """Unconfirmed quotations RAISED that day - deliberately not a payment figure.

    Kept unchanged from when sales.json counted sales instead of money, so the
    payload keeps a field it has always had. Neither dashboard reads it today,
    and it has always been excluded from every total.
    """
    start_utc, end_utc = day_bounds_utc(day)
    orders = execute(
        "sale.order", "search_read",
        [
            ["date_order", ">=", fmt_dt(start_utc)],
            ["date_order", "<", fmt_dt(end_utc)],
            ["state", "in", ["draft", "sent"]],
        ],
        fields=["amount_total"],
    )
    return {"total": round(sum(o["amount_total"] for o in orders), 2), "count": len(orders)}


def day_entry(execute, day, generated_at):
    """One day's entry for data/sales.json, in the shape the page already reads.

    `pos` and `regularSales` keep their key names but now mean "received
    through the POS" and "received through an accounting payment" - the page
    sums the two, and that sum is the day's money in.
    """
    transactions, skipped_as_pos = collect_payments(execute, day)
    return {
        "date": day.isoformat(),
        "generatedAt": generated_at,
        "pos": by_source(transactions, "pos"),
        "regularSales": by_source(transactions, "payment"),
        "pendingQuotations": pending_quotations(execute, day),
        "cash": bucket(transactions, "cash"),
        "transfer": bucket(transactions, "transfer"),
        "other": bucket(transactions, "other"),
        "products": aggregate_products(transactions),
    }, transactions, skipped_as_pos
