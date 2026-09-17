"""Offline test of odoo_payments against a canned Odoo. No credential needed.

    python scripts/test_payments.py

Covers the cases that have actually gone wrong: a POS session settlement
double counting the day's card takings, a genuine receipt sharing that same
POS journal that must survive it, an accounting payment reconciled to a POS
order's own invoice, an unreconciled advance falling back to its own
reference, an unclassified method staying out of both totals, and products
aggregating across POS and invoice lines without counting an invoice twice.
"""
import sys, json
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path(__file__).resolve().parent))
import odoo_payments as op

DAY = date(2026, 9, 16)

FIELDS = {  # what fields_get would report
    "pos.payment": ["amount", "payment_date", "payment_method_id", "pos_order_id"],
    "pos.order": ["name", "partner_id", "account_move", "state"],
    "pos.order.line": ["order_id", "product_id", "qty", "price_subtotal_incl"],
    "pos.payment.method": [
        {"id": 1, "name": "Cash", "journal_id": [10, "Cash"]},
        {"id": 2, "name": "Bank Transfer", "journal_id": [6, "Bank"]},
        {"id": 3, "name": "Customer Account", "journal_id": False},
    ],
    "account.payment": ["amount", "date", "partner_id", "journal_id", "ref", "name", "reconciled_invoice_ids"],
    "account.move.line": ["move_id", "product_id", "quantity", "price_total"],
}

DATA = {
    "pos.payment": [
        {"amount": 59.0, "payment_date": "2026-09-16 05:10:00", "payment_method_id": [1, "Cash"], "pos_order_id": [10, "A"]},
        {"amount": 77.0, "payment_date": "2026-09-16 06:20:00", "payment_method_id": [2, "Bank Transfer"], "pos_order_id": [11, "B"]},
    ],
    "pos.order": [
        # order 10 was invoiced -> its account.move is INV/2026/0001
        {"id": 10, "name": "Ike - 000151", "partner_id": False, "account_move": [900, "INV/2026/0001"]},
        {"id": 11, "name": "Ike - 000152", "partner_id": [7, "Aminath Zoona"], "account_move": False},
    ],
    "pos.order.line": [
        {"order_id": [10, "A"], "product_id": [50, "[01798] Hair comb"], "qty": 1.0, "price_subtotal_incl": 59.0},
        {"order_id": [11, "B"], "product_id": [51, "[PP9] Paper plate"], "qty": 2.0, "price_subtotal_incl": 40.0},
        {"order_id": [11, "B"], "product_id": [50, "[01798] Hair comb"], "qty": 1.0, "price_subtotal_incl": 37.0},
    ],
    "pos.payment.method": [
        {"id": 1, "name": "Cash", "journal_id": [10, "Cash"]},
        {"id": 2, "name": "Bank Transfer", "journal_id": [6, "Bank"]},
        {"id": 3, "name": "Customer Account", "journal_id": False},
    ],
    "account.payment": [
        # (a) reconciled ONLY against the POS order's own invoice -> must be skipped
        {"amount": 59.0, "partner_id": False, "journal_id": [3, "Cash"], "reconciled_invoice_ids": [900], "ref": "x", "name": "P1"},
        # (b) a real bank receipt against a separate invoice
        {"amount": 4950.0, "partner_id": [8, "GREENZONE DISTRICT"], "journal_id": [4, "Bank"], "reconciled_invoice_ids": [901], "ref": "", "name": "P2"},
        # (c) an advance, reconciled against nothing -> falls back to its own ref
        {"amount": 1000.0, "partner_id": [9, "Villa Hotels"], "journal_id": [5, "Cheque"], "reconciled_invoice_ids": [], "ref": "CUST.IN/2026/0012", "name": "P3"},
        # (d) THE BUG: a POS session settlement - POS journal, no partner, no invoice.
        #     Its 96.0 is exactly the two POS orders' takings, posted again at close.
        {"amount": 96.0, "partner_id": False, "journal_id": [6, "Bank"], "reconciled_invoice_ids": [], "ref": None, "name": "PBNK1/2026/00015"},
        # (e) THE TRAP: a genuine receipt in that SAME POS journal, with a partner
        #     and an invoice. Must survive - this is what MRH's real receipts look like.
        {"amount": 2500.0, "partner_id": [11, "Kaimoo Travels"], "journal_id": [6, "Bank"], "reconciled_invoice_ids": [901], "ref": None, "name": "PBNK1/2026/00038"},
    ],
    "account.move": [{"id": 900, "name": "INV/2026/0001"}, {"id": 901, "name": "INV/2026/0002"}],
    "account.move.line": [
        {"move_id": [901, "INV/2026/0002"], "product_id": [52, "[3C270] Foil box"], "quantity": 3.0, "price_total": 4700.0},
        {"move_id": [901, "INV/2026/0002"], "product_id": [51, "[PP9] Paper plate"], "quantity": 5.0, "price_total": 250.0},
    ],
    "sale.order": [{"amount_total": 1960.0}, {"amount_total": 500.0}],
}

def execute(model, method, *args, **kwargs):
    if method == "fields_get":
        return {f: {"type": "x"} for f in FIELDS[model]}
    rows = DATA.get(model, [])
    domain = args[0] if args else []
    for cond in domain:
        f, opr, val = cond
        if f == "id" and opr == "in":
            rows = [r for r in rows if r["id"] in val]
        elif f == "move_id" and opr == "in":
            rows = [r for r in rows if op.rel_id(r["move_id"]) in val]
        elif f == "order_id" and opr == "in":
            rows = [r for r in rows if op.rel_id(r["order_id"]) in val]
    return rows

txns, skipped = op.collect_payments(execute, DAY)
entry, _t, _s = op.day_entry(execute, DAY, "2026-09-16T07:00:00Z")

print("transactions:", len(txns), "| skipped as already-counted POS:", skipped)
for t in txns:
    print(f"  {t['ref']:<18} {t['customer']:<20} {t['amount']:>8}  {t['method']:<8} {t['source']:<8} lines={len(t['lines'])}")

print("\npos(received via POS)   :", entry["pos"])
print("regularSales(accounting):", entry["regularSales"])
print("cash / transfer / other :", entry["cash"], entry["transfer"], entry["other"])
print("pendingQuotations       :", entry["pendingQuotations"])
print("\ntransactions carried on the day entry:", len(entry["transactions"]))

# --- assertions ---
fail = []
if skipped["pos_invoice"] != 1: fail.append(f"POS-linked payment not de-duplicated: {skipped}")
if skipped["pos_settlement"] != 1: fail.append(f"POS session settlement not excluded: {skipped}")
survivor = [t for t in txns if t["amount"] == 2500.0]
if not survivor: fail.append("a GENUINE receipt in the POS journal was wrongly excluded")
elif survivor[0]["ref"] != "INV/2026/0002": fail.append(f"genuine receipt lost its invoice ref: {survivor[0]['ref']}")
if any(t["amount"] == 96.0 and t["source"] == "payment" for t in txns):
    fail.append("the session settlement is still being counted")
if entry["pos"]["total"] != 136.0: fail.append(f"POS received wrong: {entry['pos']['total']}")
if entry["regularSales"]["total"] != 8450.0: fail.append(f"accounting received wrong: {entry['regularSales']['total']} (expected 4950+1000+2500)")
if entry["cash"]["total"] != 59.0: fail.append(f"cash wrong: {entry['cash']['total']}")
if entry["transfer"]["total"] != 7527.0: fail.append(f"transfer wrong: {entry['transfer']['total']} (expected 4950+77+2500)")
if entry["other"]["total"] != 1000.0: fail.append("the Cheque payment should be in other, counted in no total")
# The day entry carries transactions now; ike-sales aggregates products from
# them. Assert the same sums hold under that derivation, including its
# once-per-reference rule, so the dashboards cannot be handed doubled goods.
agg, seen = {}, set()
for tx in sorted(entry["transactions"], key=lambda x: -x["amount"]):
    if tx["ref"] in seen:
        continue
    seen.add(tx["ref"])
    for line in tx["lines"]:
        e = agg.setdefault(line["name"], {"qty": 0.0, "total": 0.0})
        e["qty"] += line["qty"]; e["total"] += line["total"]
if "products" in entry: fail.append("day entry still carries a products array - two sources to disagree")
combs = next((v for k, v in agg.items() if "Hair comb" in k), None)
if not combs or combs["qty"] != 2.0 or combs["total"] != 96.0:
    fail.append(f"derived aggregation across two POS orders wrong: {combs}")
plates = next((v for k, v in agg.items() if "Paper plate" in k), None)
if not plates or plates["qty"] != 7.0 or plates["total"] != 290.0:
    fail.append(f"derived aggregation across POS + invoice wrong (once per reference?): {plates}")
advance = [t for t in txns if t["amount"] == 1000.0][0]
if advance["ref"] != "CUST.IN/2026/0012": fail.append("unreconciled advance did not fall back to its own reference")
if entry["pendingQuotations"] != {"total": 2460.0, "count": 2}: fail.append("pendingQuotations changed meaning")

print("\n" + ("FAILURES:\n  " + "\n  ".join(fail) if fail else "ALL ASSERTIONS PASS"))
sys.exit(1 if fail else 0)
