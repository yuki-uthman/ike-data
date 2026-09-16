#!/usr/bin/env python3
"""Pull today's customer payments from Odoo and write data/today.json.

This answers a different question from fetch_sales.py. That script counts
SALES MADE today. This one counts MONEY RECEIVED today, split by how it
arrived - cash or transfer - which is what the counter actually reconciles
at close of day. A credit sale invoiced today but settled next week appears
in sales.json today and here next week; a customer walking in to clear last
month's invoice appears here today and in sales.json last month.

Two sources, because money reaches this business two ways:
  - pos.payment      : every payment line on a POS order, dated today.
  - account.payment  : posted inbound customer payments dated today (bank
                       receipts, manual counter receipts against invoices).

De-duplication: a POS order that was also invoiced can produce an
account.payment reconciled against that same invoice. Any account.payment
whose reconciled invoices are ALL invoices already reached through a POS
order today is dropped, so such a sale is counted once, via its POS line.

Classification is name-driven and deliberately self-reporting: the payment
method (POS) or journal (accounting) name decides the bucket, the raw name
travels with every row, and anything matching neither pattern lands in
"other" - visible on the dashboard, counted in no total - rather than
silently inflating "transfer". The run log prints every distinct name seen,
so widening the patterns is a one-line change against real evidence.

Written for an Odoo instance this script cannot be tested against ahead of
time, so every optional field is probed with fields_get before it is
requested: an Odoo version that lacks one degrades that column to a
fallback instead of failing the whole run.

Runs on the same GitHub Actions schedule as fetch_sales.py; reads
ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY from the environment.
"""
import json
import os
import re
import xmlrpc.client
from datetime import datetime, timedelta, timezone
from pathlib import Path

MALDIVES_OFFSET = timedelta(hours=5)
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "today.json"

CASH_PATTERN = re.compile(r"cash", re.IGNORECASE)
TRANSFER_PATTERN = re.compile(r"transfer|bank|bml|mib|mfl|online|deposit", re.IGNORECASE)

# Odoo has renamed these across versions (posted/sent/reconciled -> in_process/paid),
# so exclude what is definitely not money rather than listing what is.
NOT_RECEIVED_STATES = ["draft", "cancel", "canceled", "cancelled"]


def maldives_day_bounds_utc(now_utc):
    maldives_now = now_utc + MALDIVES_OFFSET
    maldives_date = maldives_now.date()
    start_utc = datetime(maldives_date.year, maldives_date.month, maldives_date.day) - MALDIVES_OFFSET
    end_utc = start_utc + timedelta(days=1)
    return maldives_date, start_utc, end_utc


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

    def existing_fields(model, wanted):
        available = set(execute(model, "fields_get", [], attributes=["type"]).keys())
        return [f for f in wanted if f in available]

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    maldives_date, start_utc, end_utc = maldives_day_bounds_utc(now_utc)
    date_str = maldives_date.isoformat()

    transactions = []
    pos_invoice_ids = set()

    # ---- 1. POS payments today -------------------------------------------------
    pos_payment_fields = existing_fields(
        "pos.payment", ["amount", "payment_date", "payment_method_id", "pos_order_id"]
    )
    pos_payments = execute(
        "pos.payment", "search_read",
        [
            ["payment_date", ">=", fmt_dt(start_utc)],
            ["payment_date", "<", fmt_dt(end_utc)],
        ],
        fields=pos_payment_fields,
    )

    pos_order_ids = sorted({rel_id(p.get("pos_order_id")) for p in pos_payments} - {None})
    pos_orders = {}
    if pos_order_ids:
        order_fields = existing_fields(
            "pos.order", ["name", "partner_id", "account_move", "state"]
        )
        for order in execute(
            "pos.order", "search_read", [["id", "in", pos_order_ids]], fields=order_fields
        ):
            pos_orders[order["id"]] = order
            invoice_id = rel_id(order.get("account_move"))
            if invoice_id:
                pos_invoice_ids.add(invoice_id)

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
        })

    # ---- 2. Accounting customer payments today ---------------------------------
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

    # Resolve invoice numbers for every payment in one read.
    invoice_ids = sorted({
        move_id
        for payment in account_payments
        for move_id in (payment.get("reconciled_invoice_ids") or [])
    })
    invoice_names = {}
    if invoice_ids:
        for move in execute("account.move", "search_read", [["id", "in", invoice_ids]], fields=["name"]):
            invoice_names[move["id"]] = move["name"]

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
            payment.get("ref") or payment.get("name") or "—",
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
        })

    transactions.sort(key=lambda t: t["amount"], reverse=True)

    # ---- 3. Totals + the self-report -------------------------------------------
    def bucket(kind):
        rows = [t for t in transactions if t["method"] == kind]
        return {"total": round(sum(t["amount"] for t in rows), 2), "count": len(rows)}

    methods_seen = {}
    for t in transactions:
        entry = methods_seen.setdefault(t["rawMethod"], {"name": t["rawMethod"], "method": t["method"], "count": 0, "total": 0.0})
        entry["count"] += 1
        entry["total"] += t["amount"]
    for entry in methods_seen.values():
        entry["total"] = round(entry["total"], 2)

    payload = {
        "company": "MRH Investment",
        "currency": "MVR",
        "date": date_str,
        "generatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cash": bucket("cash"),
        "transfer": bucket("transfer"),
        "other": bucket("other"),
        "transactions": transactions,
        "methodsSeen": sorted(methods_seen.values(), key=lambda m: m["total"], reverse=True),
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    print(
        f"Wrote {DATA_PATH}: {date_str} -> "
        f"Cash {payload['cash']['total']} ({payload['cash']['count']}), "
        f"Transfer {payload['transfer']['total']} ({payload['transfer']['count']}), "
        f"Other {payload['other']['total']} ({payload['other']['count']}), "
        f"{skipped_as_pos} accounting payment(s) skipped as already-counted POS"
    )
    print("Payment method / journal names seen today:")
    for entry in payload["methodsSeen"]:
        print(f"  {entry['name']!r} -> {entry['method']} ({entry['count']} txn, {entry['total']})")
    if not payload["methodsSeen"]:
        print("  (none - no customer payments recorded yet today)")
    if payload["other"]["count"]:
        print("::warning::Unclassified payment method(s) present - widen CASH_PATTERN / TRANSFER_PATTERN in this script")


if __name__ == "__main__":
    main()
