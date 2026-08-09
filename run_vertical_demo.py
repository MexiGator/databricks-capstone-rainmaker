"""v0.2 proof-of-generalization: TWO trigger types, ONE engine, ONE pack.

    python run_vertical_demo.py

Shows a storm (event trigger) and an overdue annual check (cadence trigger)
flowing through the identical relationship stack — with the opted-out contact
held on every path. Only the VerticalPack + providers differ between verticals.
"""
from relationship_v0.packs.roofing import ROOFING_PACK
from relationship_v0.triggers.engine import run_vertical

CONTACTS = [
    {"contact_id": 100, "name": "Jo Kim", "state": "TX", "service_type": "roofing",
     "days_since_service": 400, "days_since_last_touch": 25, "opens": 4,
     "positive_replies": 1, "avg_sentiment": 0.5, "tenure_years": 4,
     "completed_jobs": 2, "lifetime_value": 9000, "exposure": 0.8},
    {"contact_id": 101, "name": "Sam Ruiz", "state": "TX", "service_type": "roofing",
     "days_since_service": 500, "is_prospect": True, "lifetime_value": 0},
    {"contact_id": 102, "name": "Pat Vega", "state": "TX", "service_type": "roofing",
     "days_since_service": 520, "opted_out": True, "tenure_years": 9,
     "completed_jobs": 6, "lifetime_value": 15000},
]
EVENTS = [{"event_type": "Severe Thunderstorm Watch", "urgency": "future",
           "states": {"TX"}, "headline": "a Severe Thunderstorm Watch",
           "area": "Dallas County, TX"}]

rows = run_vertical(ROOFING_PACK, CONTACTS, {"event": {"events": EVENTS}})

print(f"\nVertical: {ROOFING_PACK.key}  |  providers: {ROOFING_PACK.active_providers}")
print(f"{sum(r['action']['send'] for r in rows)} to send / "
      f"{sum(not r['action']['send'] for r in rows)} held  (one engine)\n" + "=" * 70)
for r in rows:
    flag = "SEND" if r["action"]["send"] else "HOLD"
    print(f"[{flag}] {r['name']:<9} {r['trigger_type']:<8} "
          f"score={r['relationship_score']:>5} {r['tier']:<4} "
          f"{r['action']['template_kind']:<17} cta={r['action']['cta_strength']:<6} "
          f"— {r['action']['reason']}")
