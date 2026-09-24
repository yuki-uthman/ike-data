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

De-duplication has two separate cases, because POS money can reach
account.payment two different ways:

  1. A POS order that was also invoiced produces an account.payment
     reconciled against that same invoice. Any account.payment whose
     reconciled invoices are ALL invoices already reached through a POS
     order that day is dropped.

  2. Closing a POS session posts ONE aggregate settlement payment per
     payment method into that method's journal - the day's card/bank
     takings as a single figure, with no partner and no invoice. That is
     the same money already counted line by line from pos.payment, so it
     must not be counted again.

Case 2 cannot be spotted by journal or by sequence: at MRH the POS
"Bank Transfer" method and ordinary customer receipts share journal 6
("Bank") and the same PBNK1 sequence. What separates them is that a
settlement has NO partner and NO reconciled invoice, while a genuine
receipt has both. So a payment in a POS-method journal with neither is
treated as a settlement and skipped.

Left uncaught, this double counts every POS card sale: 10 September 2026
read 98,499 received against 41,387 of POS takings, because the session's
41,108 settlement was added on top of the lines that made it up - and the
previous day's session, settling that morning, added 16,004 more.

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


def existing_fields(execute, model, wanted):
    available = set(execute(model, "fields_get", [], attributes=["type"]).keys())
    return [f for f in wanted if f in available]


# Odoo's own naming for a POS register's manual "Cash In/Out" wizard:
# "{session.name}-in-{reason}" / "{session.name}-out-{reason}". These entries
# land directly in account.bank.statement.line with pos_session_id set and
# have no hr.expense or account.payment behind them at all - confirmed
# 2026-09-24 across several real sessions (float pickups, petrol, gate
# passes, tea money). The free-text "reason" staff type in has no consistent
# internal structure (sometimes "reason\nStaff-name", sometimes "name\nreason",
# sometimes one line) so it is kept whole rather than split into fields.
CASH_OUT_PATTERN = re.compile(r"-out-(.*)", re.DOTALL)


def collect_cash_outs(execute, day):
    """POS till cash-out entries for one Maldives day.

    Scope is deliberately narrow: only the register's native Cash Out button,
    not hr.expense (that's ike-expenses' job) and not a general ledger dump -
    see the module docstring's de-duplication concerns, which don't apply
    here since nothing else ever reads these rows.

    Returns a list sorted by amount descending, each shaped:
        {reason, amount, method, rawMethod, time}
    `amount` is stored positive (the size of the cash-out), even though the
    underlying statement line amount is negative.
    """
    date_str = day.isoformat()
    fields = existing_fields(
        execute, "account.bank.statement.line",
        ["payment_ref", "amount", "journal_id", "pos_session_id", "create_date"],
    )
    lines = execute(
        "account.bank.statement.line", "search_read",
        [
            ["date", "=", date_str],
            ["pos_session_id", "!=", False],
            ["amount", "<", 0],
            ["payment_ref", "like", "-out-"],
        ],
        fields=fields,
    )

    cash_outs = []
    for line in lines:
        ref = line.get("payment_ref") or ""
        m = CASH_OUT_PATTERN.search(ref)
        reason = m.group(1).strip().replace("\n", " · ") if m else ref
        journal_name = rel_name(line.get("journal_id"))
        cash_outs.append({
            "reason": reason or "-",
            "amount": round(-(line.get("amount") or 0.0), 2),
            "method": classify(journal_name),
            "rawMethod": journal_name or "unknown",
            "time": local_time(line.get("create_date")),
        })
    cash_outs.sort(key=lambda c: c["amount"], reverse=True)
    return cash_outs


def bucket_out(cash_outs, kind=None):
    rows = cash_outs if kind is None else [c for c in cash_outs if c["method"] == kind]
    return {"total": round(sum(c["amount"] for c in rows), 2), "count": len(rows)}


def collect_payments(execute, day):
    """Every customer payment received on one Maldives day.

    `execute(model, method, *args, **kwargs)` is the caller's bound XML-RPC
    helper. Returns (transactions, skipped), transactions sorted by amount
    descending, each shaped:

        {ref, customer, amount, method, rawMethod, time, source, lines[]}

    `skipped` counts what was dropped as already counted, by reason:
    {"pos_invoice": n, "pos_settlement": n}.
    """
    start_utc, end_utc = day_bounds_utc(day)
    date_str = day.isoformat()

    transactions = []
    pos_invoice_ids = set()

    # Journals that POS payment methods settle into. A payment sitting in one
    # of these with no partner and no invoice is a session settlement, not a
    # customer receipt - see the module docstring.
    pos_journal_ids = {
        rel_id(m.get("journal_id"))
        for m in execute("pos.payment.method", "search_read", [], fields=["journal_id"])
    } - {None}

    # ---- 1. POS payments ----------------------------------------------------
    # Days and times come from create_date, which the SERVER stamps, never from
    # payment_date: the POS client writes that one, shifted by the difference
    # between the till's clock and the cashier's Odoo profile timezone. A
    # profile left on the wrong zone moved evening sales onto the next day
    # (Sep 2026: -8h, then +4h, on one login only). Trade-off: a sale rung up
    # offline shows the time it synced.
    pos_payment_fields = existing_fields(
        execute, "pos.payment", ["amount", "create_date", "payment_method_id", "pos_order_id"]
    )
    pos_payments = execute(
        "pos.payment", "search_read",
        [["create_date", ">=", fmt_dt(start_utc)], ["create_date", "<", fmt_dt(end_utc)]],
        fields=pos_payment_fields,
    )

    pos_order_ids = sorted({rel_id(p.get("pos_order_id")) for p in pos_payments} - {None})
    pos_orders = {}
    pos_lines_by_order = {}
    if pos_order_ids:
        order_fields = existing_fields(execute, "pos.order", ["name", "partner_id", "account_move", "state"])
        for order in execute("pos.order", "search_read", [["id", "in", pos_order_ids]], fields=order_fields):
            pos_orders[order["id"]] = order
            invoice_id = rel_id(order.get("account_move"))
            if invoice_id:
                pos_invoice_ids.add(invoice_id)

        pos_line_fields = existing_fields(
            execute, "pos.order.line", ["order_id", "product_id", "qty", "price_subtotal_incl"]
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
            "time": local_time(payment.get("create_date")),
            "source": "pos",
            "lines": sorted(
                pos_lines_by_order.get(rel_id(payment.get("pos_order_id")), []),
                key=lambda l: l["total"], reverse=True,
            ),
        })

    # ---- 2. Accounting customer payments ------------------------------------
    payment_fields = existing_fields(
        execute, "account.payment",
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

        # product_id filters out the receivable, tax and section lines in one
        # go, but NOT the COGS/inventory-valuation pair Odoo posts onto the
        # same invoice for a real-time-valuation product: those two lines
        # carry product_id too, always as a line that nets to zero (a debit
        # and a credit of the same amount). display_type="product" is what
        # actually means "a sold line", so require it explicitly. MRH turned
        # on Anglo-Saxon accounting 2026-09-21; every invoice for a
        # real-time-valuation product since then has carried this phantom
        # cancelling pair, doubling as three "line items" for one product.
        move_line_fields = existing_fields(
            execute, "account.move.line", ["move_id", "product_id", "quantity", "price_total", "display_type"]
        )
        for line in execute(
            "account.move.line", "search_read",
            [["move_id", "in", invoice_ids], ["product_id", "!=", False], ["display_type", "=", "product"]],
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

    skipped = {"pos_invoice": 0, "pos_settlement": 0}
    for payment in account_payments:
        amount = payment.get("amount") or 0.0
        if not amount:
            continue
        reconciled = payment.get("reconciled_invoice_ids") or []
        # Already counted through its POS line - drop it rather than double count.
        if reconciled and all(move_id in pos_invoice_ids for move_id in reconciled):
            skipped["pos_invoice"] += 1
            continue
        # A POS session's aggregate settlement: the same money as the
        # pos.payment rows above, posted once more when the session closed.
        if (
            rel_id(payment.get("journal_id")) in pos_journal_ids
            and not payment.get("partner_id")
            and not reconciled
        ):
            skipped["pos_settlement"] += 1
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
    return transactions, skipped


def bucket(transactions, kind):
    rows = [t for t in transactions if t["method"] == kind]
    return {"total": round(sum(t["amount"] for t in rows), 2), "count": len(rows)}


def by_source(transactions, source):
    rows = [t for t in transactions if t["source"] == source]
    return {"total": round(sum(t["amount"] for t in rows), 2), "count": len(rows)}


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

    Carries the day's transactions, which is what lets ike-sales list a day by
    invoice and customer as well as by product. The aggregated product array
    it used to write is gone: products are derivable from these lines, so
    storing both meant two things that could disagree. The dashboard does that
    aggregation now, by the same once-per-reference rule.
    """
    transactions, skipped = collect_payments(execute, day)
    cash_outs = collect_cash_outs(execute, day)
    return {
        "date": day.isoformat(),
        "generatedAt": generated_at,
        "pos": by_source(transactions, "pos"),
        "regularSales": by_source(transactions, "payment"),
        "pendingQuotations": pending_quotations(execute, day),
        "cash": bucket(transactions, "cash"),
        "transfer": bucket(transactions, "transfer"),
        "other": bucket(transactions, "other"),
        "transactions": transactions,
        "cashOut": bucket_out(cash_outs),
        "cashOutTransactions": cash_outs,
    }, transactions, skipped
