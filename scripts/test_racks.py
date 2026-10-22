"""Offline test of fetch_racks' counting rules. No credential needed.

    python scripts/test_racks.py

Covers: a product code turning into a row and a bay count; a legacy product's
height coming from its order-line text; the non-rack items mapping to their rows
and a part (a stand, a bracket) mapping to nothing; a till line that repeats a
sale-order line being recognised, and a refund never being one; the Maldives
(UTC+5) day rollover; and the file growing one month column on its own while
keeping the static Zoho history aligned.
"""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_racks as f

# --- product -> row ------------------------------------------------------
assert f.classify(1, "2C-WHT-DS-2000-X03", "#2C White Double Side 2000mm x3") == ("2C White Double Side 2000mm", 3)
assert f.classify(2, "RD8-GRY-SSP-1950-X01", "#RD8 Gray Single Perforated 1950mm x1") == ("RD8 Gray Single Side 1950mm", 1)
assert f.classify(3, "RD8-WHT-ERF-1550", "#RD8 White End Rack Flat 1550mm") == ("RD8 White End Rack 1550mm", 1), "no -Xnn means one bay"
assert f.classify(4, "2C-BLK-SHTOP-900x300", "2C Black Shelf Top 900x300mm") == ("2C Black top shelf 900mm", 1)
assert f.classify(5, "RD8-GRY-SHTOP-700", "RD8 Gray Shelf Top 700x300mm") == ("RD8 Gray top shelf 700mm", 1)
assert f.classify(6, "RACKLKHM", "Lui Kaana Haru Medium 5 Layers") == ("Lui Kaana Haru Medium 5 Layers", 1)
assert f.classify(7, "70187", "Trolley Stand 5 Layer White") == ("Trolley Stand 5 Layer White", 1)
assert f.classify(8, "", "2C Price Border Strip Red") == ("2C Price border · Red", 1)
assert f.classify(9, "70218", "RD8 Price Border Strip  Green 900mm (875)") == ("RD8 Price border · Green", 1), "double space in the real name"
assert f.classify(10, "", "2C White Steel Barrier 900mm") == ("2C Steel barrier 900mm · White", 1)
assert f.classify(11, "2C-WHT-STAND-2000", "2C White Stand 2000mm") is None, "a stand is a part, not a rack"
assert f.classify(12, "2C-BLK-BRKTF-900", "2C Black Bracket Front 900mm") is None
assert f.classify(13, "", "2C White End Round Shelves") is None, "round-shelf end racks are left out"
assert f.classify(1459, "", "2C ER Round W") is None, "so is the old legacy round-shelf end rack"

# --- a legacy product: the line text can set the height -------------------
assert f.legacy_row(1439, "2C Double Side White 6.5ft X1set") == ("2C White Double Side 2000mm", 1), "text says 6.5ft"
assert f.legacy_row(1439, "2C Double Side White 6ft X1set") == ("2C White Double Side 1800mm", 1)
assert f.legacy_row(1439, "2C DS W 1set") == ("2C White Double Side 1800mm", 1), "no height in the text: the default"
assert f.legacy_row(1467, None) == ("2C White Double Side 1800mm", 2), "a missing text falls back to the default"
assert f.legacy_row(1521, "8 Single Side White 5ft X7set") == ("RD8 White Single Side 1550mm", 7)
assert f.legacy_row(1521, "8 Single Side White 6.5ft X7set") == ("RD8 White Single Side 1950mm", 7), "6.5ft must not read as 5ft"

# --- one sale entered twice ----------------------------------------------
t0 = datetime(2026, 9, 1, 4, 52)
seen = {f.sale_key(55, 1215, 1.0, 1900.0): [t0]}
assert f.is_repeat(seen, 55, 1215, 1.0, 1900.0, datetime(2026, 9, 1, 4, 54)), "same sale, two minutes later"
assert not f.is_repeat(seen, 55, 1215, 1.0, 1900.0, datetime(2026, 9, 1, 7, 0)), "two hours later is a new sale"
assert not f.is_repeat(seen, 56, 1215, 1.0, 1900.0, datetime(2026, 9, 1, 4, 54)), "another customer"
assert not f.is_repeat(seen, 55, 1215, 2.0, 1900.0, datetime(2026, 9, 1, 4, 54)), "another quantity"
assert not f.is_repeat(seen, 55, 1215, 1.0, 1800.0, datetime(2026, 9, 1, 4, 54)), "another price"
assert not f.is_repeat(seen, 55, 1215, -1.0, 1900.0, datetime(2026, 9, 1, 4, 54)), "a refund is never a repeat"

# --- days and months -----------------------------------------------------
assert f.maldives_day("2026-10-21 22:30:00") == "2026-10-22", "UTC+5 rollover"
assert f.month_list("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]

lines = [
    {"row": "A", "racks": 3.0, "date": "2026-10-21"},
    {"row": "A", "racks": -1.0, "date": "2026-10-22"},  # a till refund subtracts
    {"row": "A", "racks": 2.0, "date": "2026-09-30"},
    {"row": "B", "racks": 4.0, "date": "2026-09-30"},
]
agg = f.aggregate(lines)
assert dict(agg["A"]) == {"2026-10": 2.0, "2026-09": 2.0} and dict(agg["B"]) == {"2026-09": 4.0}, agg

# --- the file: static Zoho history + live Odoo, month axis growing --------
history = {
    "months": ["2026-08", "2026-09", "2026-10"],
    "products": [
        {"series": "2C", "name": "A", "zoho": [5, 0, 0]},
        {"series": "2C", "name": "B", "zoho": [0, 0, 0]},
    ],
}
payload, warn = f.build_payload(history, agg, datetime(2026, 10, 21, 10, 0))
assert payload["months"] == ["2026-08", "2026-09", "2026-10"], "still in the last history month"
assert [p["odoo"] for p in payload["products"]] == [[0, 2, 2], [0, 4, 0]], payload["products"]
assert warn == [], warn

# Nov 2026 arrives (a Maldives date, so 20:00 UTC on 31 Oct already counts): one new column.
payload, _ = f.build_payload(history, agg, datetime(2026, 10, 31, 20, 0))
assert payload["months"] == ["2026-08", "2026-09", "2026-10", "2026-11"], payload["months"]
assert payload["products"][0]["zoho"] == [5, 0, 0, 0], "Zoho history is padded, never shifted"
assert all(len(p["odoo"]) == len(p["zoho"]) == 4 for p in payload["products"])

# A row Odoo sold but the page has no row for is reported, not silently dropped.
extra = dict(agg)
extra["Z"] = {"2026-10": 1.0}
_, warn = f.build_payload(history, extra, datetime(2026, 10, 21, 10, 0))
assert len(warn) == 1 and "Z" in warn[0], warn

print("ok")
