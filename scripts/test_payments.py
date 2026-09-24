"""Offline test of odoo_payments against a canned Odoo. No credential needed.

    python scripts/test_payments.py

Covers the cases that have actually gone wrong: a POS session settlement
double counting the day's card takings, a genuine receipt sharing that same
POS journal that must survive it, an accounting payment reconciled to a POS
order's own invoice, an unreconciled advance falling back to its own
reference, an unclassified method staying out of both totals, products
aggregating across POS and invoice lines without counting an invoice twice,
and a real-time-valuation product's COGS/inventory-valuation line pair (same
product_id, amounts that cancel to zero) not being mistaken for a second and
third sold line - see INV/2026/00187, 2026-09-21, once MRH turned on
Anglo-Saxon accounting. Also covers till cash-out: the native POS Cash Out
button's "-out-" statement lines (no hr.expense/account.payment behind them
at all - ike-pos's only source for them), a same-day "-in-" top-up that must
NOT be counted as an out, and a different day's cash-out that must not leak
into this day's total. Also covers the till reconciliation card
(collect_till): a closed session's opening/expected/counted/difference, a
different day's session not leaking in, and - the one that actually bit a
same-day open session once - counted/difference coming back None rather
than the garbage cash_register_balance_end_real=0 Odoo reports before a
session is closed.
"""
import sys, json
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path(__file__).resolve().parent))
import odoo_payments as op

DAY = date(2026, 9, 16)

FIELDS = {  # what fields_get would report
    "pos.payment": ["amount", "create_date", "payment_method_id", "pos_order_id"],
    "pos.order": ["name", "partner_id", "account_move", "state"],
    "pos.order.line": ["order_id", "product_id", "qty", "price_subtotal_incl"],
    "pos.payment.method": [
        {"id": 1, "name": "Cash", "journal_id": [10, "Cash"]},
        {"id": 2, "name": "Bank Transfer", "journal_id": [6, "Bank"]},
        {"id": 3, "name": "Customer Account", "journal_id": False},
    ],
    "account.payment": ["amount", "date", "partner_id", "journal_id", "ref", "name", "reconciled_invoice_ids"],
    "account.move.line": ["move_id", "product_id", "quantity", "price_total"],
    "account.bank.statement.line": ["payment_ref", "amount", "journal_id", "pos_session_id", "create_date"],
    "pos.session": ["name", "state", "start_at", "stop_at", "cash_register_balance_start",
                    "cash_register_balance_end", "cash_register_balance_end_real", "cash_register_difference"],
}

DATA = {
    "pos.payment": [
        {"amount": 59.0, "create_date": "2026-09-16 05:10:00", "payment_date": "2026-09-16 09:10:00", "payment_method_id": [1, "Cash"], "pos_order_id": [10, "A"]},
        {"amount": 77.0, "create_date": "2026-09-16 06:20:00", "payment_date": "2026-09-15 22:20:00", "payment_method_id": [2, "Bank Transfer"], "pos_order_id": [11, "B"]},
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
        {"move_id": [901, "INV/2026/0002"], "product_id": [52, "[3C270] Foil box"], "quantity": 3.0, "price_total": 4700.0, "display_type": "product"},
        {"move_id": [901, "INV/2026/0002"], "product_id": [51, "[PP9] Paper plate"], "quantity": 5.0, "price_total": 250.0, "display_type": "product"},
        # THE BUG: Odoo's own COGS/inventory-valuation pair for a real-time
        # costed product, posted onto this same invoice. Same product_id as a
        # genuine sale would have, amounts that net to zero - must not appear
        # as a phantom second and third line for "Foil box".
        {"move_id": [901, "INV/2026/0002"], "product_id": [52, "[3C270] Foil box"], "quantity": 1.0, "price_total": 867.0, "display_type": "cogs"},
        {"move_id": [901, "INV/2026/0002"], "product_id": [52, "[3C270] Foil box"], "quantity": 1.0, "price_total": -867.0, "display_type": "cogs"},
    ],
    "sale.order": [{"amount_total": 1960.0}, {"amount_total": 500.0}],
    "account.bank.statement.line": [
        # (a) a real till cash-out, same day - must be counted.
        {"date": "2026-09-16", "amount": -85.0, "payment_ref": "Ike/00028-out-Central Pickup\nStaff-Murshid",
         "journal_id": [10, "Cash"], "pos_session_id": [47, "Ike/00028"], "create_date": "2026-09-16 03:30:00"},
        # (b) a same-day cash IN (float top-up) via the same wizard - must NOT
        #     be counted as an out (wrong sign, and its ref has no "-out-").
        {"date": "2026-09-16", "amount": 200.0, "payment_ref": "Ike/00028-in-Float top-up",
         "journal_id": [10, "Cash"], "pos_session_id": [47, "Ike/00028"], "create_date": "2026-09-16 03:00:00"},
        # (c) a genuine cash-out, but on a DIFFERENT day - must not leak in.
        {"date": "2026-09-15", "amount": -50.0, "payment_ref": "Ike/00027-out-Petrol",
         "journal_id": [10, "Cash"], "pos_session_id": [46, "Ike/00027"], "create_date": "2026-09-15 10:00:00"},
        # (d) an unrelated Cash-journal entry with no pos_session_id at all -
        #     not a till cash-out, must be excluded even though it is negative.
        {"date": "2026-09-16", "amount": -30.0, "payment_ref": "Bank charge",
         "journal_id": [10, "Cash"], "pos_session_id": False, "create_date": "2026-09-16 06:00:00"},
    ],
    "pos.session": [
        # DAY (2026-09-16): two sessions in one day. Opening must come from
        # the EARLIEST session's start; expected/counted/difference from the
        # LATEST session's own figures (its running balance already carries
        # the whole day, not just its own slice of it).
        {"name": "Ike/A1", "state": "closed", "start_at": "2026-09-16 03:00:00", "stop_at": "2026-09-16 09:00:00",
         "cash_register_balance_start": 500.0, "cash_register_balance_end": 600.0,
         "cash_register_balance_end_real": 600.0, "cash_register_difference": 0.0},
        {"name": "Ike/A2", "state": "closed", "start_at": "2026-09-16 10:00:00", "stop_at": "2026-09-16 18:00:00",
         "cash_register_balance_start": 600.0, "cash_register_balance_end": 700.0,
         "cash_register_balance_end_real": 695.0, "cash_register_difference": -5.0},
        # a different day's session - must not leak into DAY's till figures.
        {"name": "Ike/PREV", "state": "closed", "start_at": "2026-09-15 04:00:00", "stop_at": "2026-09-15 18:00:00",
         "cash_register_balance_start": 100.0, "cash_register_balance_end": 200.0,
         "cash_register_balance_end_real": 200.0, "cash_register_difference": 0.0},
        # 2026-09-17: still open. counted/difference must come back None, not
        # the garbage Odoo reports before a session is actually closed
        # (balance_end_real 0, a difference computed against that 0).
        {"name": "Ike/OPEN", "state": "opened", "start_at": "2026-09-17 04:00:00", "stop_at": False,
         "cash_register_balance_start": 700.0, "cash_register_balance_end": 850.0,
         "cash_register_balance_end_real": 0.0, "cash_register_difference": -850.0},
    ],
}

POS_PAYMENT_DOMAINS = []

def execute(model, method, *args, **kwargs):
    if model == "pos.payment" and method == "search_read":
        POS_PAYMENT_DOMAINS.append(args[0])
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
        elif f == "display_type" and opr == "=":
            rows = [r for r in rows if r.get("display_type") == val]
        # account.bank.statement.line only - account.payment's own "date"
        # condition is deliberately left unhandled above/unfiltered, as it
        # always was: its canned rows carry no date field at all.
        elif model == "account.bank.statement.line" and f == "date" and opr == "=":
            rows = [r for r in rows if r.get("date") == val]
        elif model == "account.bank.statement.line" and f == "pos_session_id" and opr == "!=" and val is False:
            rows = [r for r in rows if r.get("pos_session_id")]
        elif model == "account.bank.statement.line" and f == "amount" and opr == "<":
            rows = [r for r in rows if (r.get("amount") or 0) < val]
        elif model == "account.bank.statement.line" and f == "payment_ref" and opr == "like":
            rows = [r for r in rows if val in (r.get("payment_ref") or "")]
        elif model == "pos.session" and f == "start_at" and opr == ">=":
            rows = [r for r in rows if (r.get("start_at") or "") >= val]
        elif model == "pos.session" and f == "start_at" and opr == "<":
            rows = [r for r in rows if (r.get("start_at") or "") < val]
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
# The POS day and time must come from the server-stamped create_date. The
# canned payment_date is deliberately skewed (+4h / -7h): a profile-timezone
# fault shifts that field and must not move a sale between days or clock times.
if any(cond[0] != "create_date" for cond in POS_PAYMENT_DOMAINS[0]):
    fail.append(f"POS day filter uses something other than create_date: {POS_PAYMENT_DOMAINS[0]}")
cash_tx = [t for t in txns if t["source"] == "pos" and t["method"] == "cash"][0]
if cash_tx["time"] != "10:10": fail.append(f"POS time not taken from create_date: {cash_tx['time']} (expected 10:10)")
# THE BUG (2026-09-21, INV/2026/00187): a real-time-valuation product's own
# COGS/inventory-valuation pair, same product_id, amounts that cancel to
# zero - must not surface as extra "Foil box" line items alongside the one
# genuine sale.
foil_lines = [l for l in survivor[0]["lines"] if "Foil box" in l["name"]]
if len(foil_lines) != 1 or foil_lines[0]["total"] != 4700.0:
    fail.append(f"COGS pair leaked into transaction lines as phantom Foil box entries: {foil_lines}")
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

# --- till cash-out ---
cash_outs = op.collect_cash_outs(execute, DAY)
print("\ncash outs:", cash_outs)
if entry["cashOut"] != {"total": 85.0, "count": 1}:
    fail.append(f"cashOut wrong: {entry['cashOut']} (expected only the same-day -out- line, 85.0/1)")
if len(cash_outs) != 1 or cash_outs[0]["amount"] != 85.0:
    fail.append(f"collect_cash_outs returned the wrong rows: {cash_outs}")
elif cash_outs[0]["reason"] != "Central Pickup · Staff-Murshid":
    fail.append(f"cash-out reason not extracted/joined correctly: {cash_outs[0]['reason']!r}")
if any(c["amount"] == 200.0 for c in cash_outs):
    fail.append("a same-day cash IN (-in-) was wrongly counted as a cash-out")
if any(c["amount"] == 50.0 for c in cash_outs):
    fail.append("a different day's cash-out leaked into this day's total")
if any(c["amount"] == 30.0 for c in cash_outs):
    fail.append("a non-POS Cash-journal entry (no pos_session_id) was wrongly counted as a till cash-out")

# --- till reconciliation ---
till = entry["till"]
print("\ntill:", till)
expected_till = {
    "opening": 500.0, "expectedClosing": 700.0, "counted": 695.0, "difference": -5.0,
    "closed": True, "sessionCount": 2,
}
if till != expected_till:
    fail.append(f"collect_till wrong for a two-session day: {till} (expected {expected_till})")

prev_day_till = op.collect_till(execute, date(2026, 9, 15))
if not prev_day_till or prev_day_till["opening"] != 100.0 or prev_day_till["sessionCount"] != 1:
    fail.append(f"a different day's session leaked into/out of collect_till: {prev_day_till}")

open_day_till = op.collect_till(execute, date(2026, 9, 17))
print("still-open day till:", open_day_till)
if not open_day_till or open_day_till["closed"]:
    fail.append(f"a still-open session was reported as closed: {open_day_till}")
elif open_day_till["counted"] is not None or open_day_till["difference"] is not None:
    fail.append(
        f"counted/difference should be None while the session is still open "
        f"(Odoo's own balance_end_real/difference are garbage until close): {open_day_till}"
    )
elif open_day_till["expectedClosing"] != 850.0:
    fail.append(f"expectedClosing should still show the live running balance while open: {open_day_till}")

no_session_till = op.collect_till(execute, date(2026, 9, 25))
if no_session_till is not None:
    fail.append(f"a day with no session at all should return None, got {no_session_till}")

print("\n" + ("FAILURES:\n  " + "\n  ".join(fail) if fail else "ALL ASSERTIONS PASS"))
sys.exit(1 if fail else 0)
