#!/usr/bin/env python3
"""Print every customer payment Odoo holds for one day. READ ONLY - writes nothing.

For answering "why is that day's number what it is". Shows each POS and
accounting payment as the pipeline sees it, plus the raw fields the totals
are derived from, so a surprising figure can be traced to a record rather
than argued about.

Usage:
    python scripts/inspect_day.py 2026-09-10

Reads ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY from the environment.
"""
import os
import sys
import xmlrpc.client
from datetime import date

import odoo_payments as op


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: inspect_day.py <YYYY-MM-DD>")
    day = date.fromisoformat(sys.argv[1])

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

    available = set(execute("account.payment", "fields_get", [], attributes=["type"]).keys())
    wanted = [
        "name", "ref", "amount", "date", "partner_id", "journal_id", "state",
        "payment_type", "partner_type", "reconciled_invoice_ids", "is_internal_transfer",
        "move_id", "is_reconciled", "payment_method_line_id",
    ]
    fields = [f for f in wanted if f in available]
    print(f"account.payment fields NOT present in this Odoo: {sorted(set(wanted) - available) or 'none'}\n")

    payments = execute(
        "account.payment", "search_read",
        [
            ["date", "=", day.isoformat()],
            ["payment_type", "=", "inbound"],
            ["partner_type", "=", "customer"],
            ["state", "not in", op.NOT_RECEIVED_STATES],
        ],
        fields=fields,
    )
    print(f"=== account.payment on {day} (inbound, customer, not draft/cancelled): {len(payments)} ===")
    for p in payments:
        print(f"\n  {p.get('name')}  amount={p.get('amount')}  state={p.get('state')}")
        print(f"    partner  : {op.rel_name(p.get('partner_id'))}")
        print(f"    journal  : {op.rel_name(p.get('journal_id'))} -> classified {op.classify(op.rel_name(p.get('journal_id')))}")
        print(f"    ref      : {p.get('ref')!r}")
        print(f"    reconciled_invoice_ids: {p.get('reconciled_invoice_ids')}")
        for extra in ("is_reconciled", "is_internal_transfer"):
            if extra in p:
                print(f"    {extra}: {p.get(extra)}")
        inv_ids = p.get("reconciled_invoice_ids") or []
        if inv_ids:
            for move in execute("account.move", "search_read", [["id", "in", inv_ids]],
                                fields=["name", "invoice_date", "amount_total", "payment_state", "move_type"]):
                print(f"      invoice {move['name']}  dated {move.get('invoice_date')}  "
                      f"total {move.get('amount_total')}  {move.get('payment_state')}  {move.get('move_type')}")
        else:
            print("      (no invoice - contributes no product lines, and its ref falls back)")

    transactions, skipped = op.collect_payments(execute, day)
    pos_rows = [t for t in transactions if t["source"] == "pos"]
    pay_rows = [t for t in transactions if t["source"] == "payment"]
    print(f"\n=== as the pipeline sees it ===")
    print(f"  POS payments       : {len(pos_rows):>3}  {round(sum(t['amount'] for t in pos_rows), 2)}")
    print(f"  accounting payments: {len(pay_rows):>3}  {round(sum(t['amount'] for t in pay_rows), 2)}")
    print(f"  skipped as already-counted POS: {skipped}")
    print(f"  day total          : {round(sum(t['amount'] for t in transactions), 2)}")
    print(f"  rows with no line detail: {sum(1 for t in transactions if not t['lines'])}")


if __name__ == "__main__":
    main()
