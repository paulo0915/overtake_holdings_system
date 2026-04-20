import csv, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
with open("output/sothebys_motorsport_auction_results.csv", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))
cols = ["VIN","Mileage_mi","Ext_color","Int_color","Engine_spec","Transmission","Country","Lot_number","Bid_count","Sold_status"]
print("--- Field fill-rate (%d rows) ---" % len(rows))
for c in cols:
    filled = sum(1 for r in rows if (r.get(c) or "").strip())
    print(f"  {c}: {filled}/{len(rows)}")
print()
print("--- Country distribution ---")
for k, v in sorted(collections.Counter((r.get("Country") or "(empty)") for r in rows).items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")
print()
print("--- Mileage filled rows ---")
for r in rows:
    if (r.get("Mileage_mi") or "").strip():
        print(f"  {r['Title']} | {r['Mileage_mi']} mi")
print()
print("--- Transmission distribution ---")
for k, v in sorted(collections.Counter((r.get("Transmission") or "(empty)") for r in rows).items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")
