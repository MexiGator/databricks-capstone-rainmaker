"""
Rainmaker -- seed the static data.

Seeds three things:
  1. event_service_map    -- which NWS alert drives which service line
  2. customers            -- ~55 customers + prospects, 6 tenants, national spread
  3. outreach_templates   -- 7 past campaigns; this is the RAG corpus

DETERMINISTIC BY DESIGN. random.seed(RANDOM_SEED) means every run produces
identical rows, so tests can assert on exact values and the demo is repeatable.

IDEMPOTENT. Everything UPSERTs on the primary key, so re-running never
duplicates and never wipes live opportunity data.

Run:
    import os; os.environ["LAKEBASE_URL"] = "..."
    import lakebase, seed
    lakebase.ensure_schema()
    seed.run()
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from psycopg2.extras import execute_values

import lakebase

RANDOM_SEED = 42

# =====================================================================
# 1. EVENT -> SERVICE MAP
# Real NWS event names (must match the `event` field the API returns).
# Many-to-many: a hurricane creates roofing AND restoration demand.
# urgency_weight is the multiplier Match & Score uses (0..1).
# =====================================================================
EVENT_SERVICE_MAP: list[tuple[str, str, float, str]] = [
    # event_type,                  service,        urgency, damage_mode
    ("Severe Thunderstorm Warning", "roofing",      0.85, "hail impact / shingle bruising"),
    ("Tornado Warning",             "roofing",      1.00, "structural wind damage"),
    ("High Wind Warning",           "roofing",      0.75, "shingle loss / flashing lift"),
    ("Winter Storm Warning",        "plumbing",     0.80, "frozen supply lines"),
    ("Winter Storm Warning",        "roofing",      0.55, "ice dams / gutter load"),
    ("Ice Storm Warning",           "roofing",      0.80, "ice dams / gutter collapse"),
    ("Hard Freeze Warning",         "plumbing",     0.95, "burst pipes"),
    ("Extreme Cold Warning",        "plumbing",     0.90, "burst pipes"),
    ("Flood Warning",               "restoration",  0.85, "standing water / substrate saturation"),
    ("Flash Flood Warning",         "restoration",  1.00, "rapid intrusion / mould risk"),
    ("Excessive Heat Warning",      "hvac",         0.90, "compressor failure under load"),
    ("Extreme Heat Warning",        "hvac",         0.90, "compressor failure under load"),
    ("Heat Advisory",               "hvac",         0.60, "capacity strain"),
    ("Hurricane Warning",           "roofing",      1.00, "wind uplift / envelope breach"),
    ("Hurricane Warning",           "restoration",  0.95, "water intrusion"),
    ("Tropical Storm Warning",      "restoration",  0.80, "water intrusion"),
    ("Tropical Storm Warning",      "roofing",      0.70, "wind uplift"),
]

# =====================================================================
# 2. TENANTS + SERVICE AREAS
# Each tenant is a regional home-services company Analytic Gator runs
# Rainmaker for. Cities are placed where their hazard actually occurs --
# that is what makes the demo never empty: some NWS alert is always live
# over someone's footprint.
# =====================================================================
TENANTS: dict[str, dict] = {
    "summit-exteriors": {
        "service": "roofing",
        "reps": ["Dana Ramirez", "Kyle Whitfield", "Joseph Okafor"],
        "cities": [
            ("Dallas", "TX", "75201", 32.7767, -96.7970),
            ("Fort Worth", "TX", "76102", 32.7555, -97.3308),
            ("Plano", "TX", "75024", 33.0198, -96.6989),
            ("Oklahoma City", "OK", "73102", 35.4676, -97.5164),
            ("Tulsa", "OK", "74103", 36.1540, -95.9928),
            ("Denver", "CO", "80202", 39.7392, -104.9903),
            ("Colorado Springs", "CO", "80903", 38.8339, -104.8214),
            ("Wichita", "KS", "67202", 37.6872, -97.3301),
            ("Amarillo", "TX", "79101", 35.2220, -101.8313),
        ],
    },
    "heartland-roofing": {
        "service": "roofing",
        "reps": ["Maggie Pearson", "Trong Nguyen"],
        "cities": [
            ("Omaha", "NE", "68102", 41.2565, -95.9345),
            ("Lincoln", "NE", "68508", 40.8136, -96.7026),
            ("Des Moines", "IA", "50309", 41.5868, -93.6250),
            ("Kansas City", "MO", "64106", 39.0997, -94.5786),
            ("Springfield", "MO", "65806", 37.2090, -93.2923),
            ("Topeka", "KS", "66603", 39.0473, -95.6752),
        ],
    },
    "northline-plumbing": {
        "service": "plumbing",
        "reps": ["Stefan Kowalski", "Aoife Brennan", "Ravi Iyer"],
        "cities": [
            ("Minneapolis", "MN", "55401", 44.9778, -93.2650),
            ("Madison", "WI", "53703", 43.0731, -89.4012),
            ("Milwaukee", "WI", "53202", 43.0389, -87.9065),
            ("Chicago", "IL", "60601", 41.8781, -87.6298),
            ("Buffalo", "NY", "14202", 42.8864, -78.8784),
            ("Rochester", "NY", "14604", 43.1566, -77.6088),
            ("Hartford", "CT", "06103", 41.7658, -72.6734),
            ("Boston", "MA", "02108", 42.3601, -71.0589),
        ],
    },
    "gulfstate-restoration": {
        "service": "restoration",
        "reps": ["Celeste Boudreaux", "Lonnie Hargrove"],
        "cities": [
            ("Houston", "TX", "77002", 29.7604, -95.3698),
            ("New Orleans", "LA", "70112", 29.9511, -90.0715),
            ("Baton Rouge", "LA", "70802", 30.4515, -91.1871),
            ("Mobile", "AL", "36602", 30.6954, -88.0399),
            ("Tampa", "FL", "33602", 27.9506, -82.4572),
            ("Charleston", "SC", "29401", 32.7765, -79.9311),
            ("Nashville", "TN", "37203", 36.1627, -86.7816),
        ],
    },
    "desert-air-hvac": {
        "service": "hvac",
        "reps": ["Pilar Salazar", "Vikram Chandra", "Bea Mott"],
        "cities": [
            ("Phoenix", "AZ", "85004", 33.4484, -112.0740),
            ("Tucson", "AZ", "85701", 32.2226, -110.9747),
            ("Las Vegas", "NV", "89101", 36.1699, -115.1398),
            ("Albuquerque", "NM", "87102", 35.0844, -106.6504),
            ("Austin", "TX", "78701", 30.2672, -97.7431),
            ("San Antonio", "TX", "78205", 29.4241, -98.4936),
            ("Fresno", "CA", "93721", 36.7378, -119.7871),
            ("Orlando", "FL", "32801", 28.5383, -81.3792),
        ],
    },
    "atlantic-exteriors": {
        "service": "roofing",
        "reps": ["Genevieve Delacroix", "Hassan Amari"],
        "cities": [
            ("Miami", "FL", "33130", 25.7617, -80.1918),
            ("Jacksonville", "FL", "32202", 30.3322, -81.6557),
            ("Savannah", "GA", "31401", 32.0809, -81.0912),
            ("Wilmington", "NC", "28401", 34.2257, -77.9447),
            ("Virginia Beach", "VA", "23451", 36.8529, -75.9780),
            ("Corpus Christi", "TX", "78401", 27.8006, -97.3964),
        ],
    },
}

# Typical job value by service line -- drives est_job_value and est_value.
VALUE_RANGE: dict[str, tuple[int, int]] = {
    "roofing": (9_000, 32_000),
    "restoration": (3_500, 26_000),
    "hvac": (4_000, 13_000),
    "plumbing": (600, 7_500),
}

FIRST_NAMES = [
    "Marcus", "Denise", "Priya", "Curtis", "Yolanda", "Trent", "Alicia", "Rafael",
    "Bethany", "Omar", "Shannon", "Devin", "Rosalind", "Nathaniel", "Camille",
    "Grady", "Imani", "Vaughn", "Lorraine", "Desmond", "Fiona", "Malcolm",
    "Serena", "Hollis", "Adeline", "Bruno", "Tessa", "Reggie", "Nadia", "Wesley",
]
LAST_NAMES = [
    "Alvarez", "Whitmore", "Okonkwo", "Barrett", "Delgado", "Fairchild", "Nakamura",
    "Prescott", "Vasquez", "Sinclair", "Abernathy", "Moreau", "Castellanos",
    "Radcliffe", "Osei", "Lindqvist", "Beaumont", "Thackeray", "Solano", "Vandergriff",
]


def _tier(lifetime_value: float, is_prospect: bool) -> str:
    if is_prospect:
        return "standard"
    if lifetime_value >= 45_000:
        return "platinum"
    if lifetime_value >= 18_000:
        return "gold"
    return "standard"


def build_customers() -> list[tuple]:
    """Generate the CRM rows. Pure function -- deterministic, testable."""
    rng = random.Random(RANDOM_SEED)
    rows: list[tuple] = []
    seq = 0
    today = date(2026, 8, 7)

    for tenant, cfg in TENANTS.items():
        service = cfg["service"]
        lo, hi = VALUE_RANGE[service]

        for city, state, zip_code, lat, lon in cfg["cities"]:
            # 1-2 records per city, ~25% of them prospects (no history yet)
            for _ in range(rng.choice([1, 1, 2])):
                seq += 1
                is_prospect = rng.random() < 0.25

                # jitter the coordinates so records aren't stacked on one point
                c_lat = round(lat + rng.uniform(-0.18, 0.18), 5)
                c_lon = round(lon + rng.uniform(-0.18, 0.18), 5)

                est_job_value = float(rng.randint(lo, hi))

                if is_prospect:
                    contract_value = 0.0
                    lifetime_value = 0.0
                    tenure_start = None
                    status = "lead"
                else:
                    contract_value = est_job_value
                    lifetime_value = round(contract_value * rng.uniform(1.0, 2.8), 2)
                    tenure_start = today - timedelta(days=rng.randint(120, 2_600))
                    status = "active"

                first = rng.choice(FIRST_NAMES)
                last = rng.choice(LAST_NAMES)
                name = f"{first} {last}"
                handle = f"{first[0].lower()}{last.lower()}{seq:03d}"

                rows.append(
                    (
                        f"cust_{seq:04d}",
                        tenant,
                        name,
                        f"{handle}@example.com",
                        f"+1555{rng.randint(1000000, 9999999)}",
                        city,
                        state,
                        zip_code,
                        c_lat,
                        c_lon,
                        service,
                        contract_value,
                        lifetime_value,
                        est_job_value,
                        tenure_start,
                        _tier(lifetime_value, is_prospect),
                        rng.choice(cfg["reps"]),
                        status,
                        is_prospect,
                    )
                )
    return rows


# =====================================================================
# 3. OUTREACH TEMPLATES -- the RAG corpus.
# Real campaign copy with a measured booked-rate. This is genuine
# unstructured text: it gets chunked, embedded, and retrieved so the
# agent grounds each draft on the best-performing past message.
# Placeholders {first_name} / {city} / {event_headline} are filled at draft time.
# =====================================================================
OUTREACH_TEMPLATES: list[tuple[str, str, str, str, str, float, int]] = [
    (
        "tpl_hail_roof",
        "Hail Alley — 48-Hour Priority Roof Inspection",
        "Severe Thunderstorm Warning",
        "roofing",
        "Hi {first_name} — the storm cell that moved through {city} carried hail large "
        "enough to bruise asphalt shingles. Bruising is the damage homeowners never see "
        "from the ground: the granule layer fractures, the mat underneath starts wicking "
        "moisture, and the leak shows up four to six months later once it has already "
        "reached the decking. Most carriers also run a claim window that closes about a "
        "year after the storm date. We are holding priority inspection slots for existing "
        "customers in your area over the next 48 hours. It takes about 30 minutes, we "
        "photograph every slope, and you get a written damage report you can hand straight "
        "to your adjuster. Reply YES and we will lock in a time.",
        0.34,
        1_240,
    ),
    (
        "tpl_wind_roof",
        "Post-Windstorm Shingle & Flashing Check",
        "High Wind Warning",
        "roofing",
        "Hi {first_name} — sustained winds through {city} were strong enough to lift "
        "shingle tabs and pull flashing away from the chimney and valley lines. Wind "
        "damage is deceptive: the shingle often falls back into place looking untouched "
        "while the seal strip underneath is broken, so the next driving rain goes straight "
        "into the decking. We are running free wind-damage checks for homes in your zip "
        "this week. Our crew documents lifted tabs, missing ridge caps, and any separated "
        "flashing, and we tell you honestly if there is nothing to fix. Reply YES for a slot.",
        0.29,
        860,
    ),
    (
        "tpl_freeze_pipe",
        "Hard Freeze — Pre-Emptive Pipe Burst Check",
        "Hard Freeze Warning",
        "plumbing",
        "Hi {first_name} — {city} is under a hard freeze and we are already booking "
        "emergency calls. The pipes that fail are almost always the same ones: exterior "
        "hose bibs, lines in unheated crawlspaces, and anything running through an "
        "uninsulated garage wall. A burst half-inch line puts out several hundred gallons "
        "an hour, and the water damage costs many times what the plumbing repair does. "
        "We can send a technician to pressure-check and insulate the vulnerable runs "
        "before the coldest night. Same-day slots are limited. Reply YES and we will get "
        "you on the board.",
        0.41,
        1_510,
    ),
    (
        "tpl_ice_dam",
        "Ice Storm — Ice Dam & Gutter Load Assessment",
        "Ice Storm Warning",
        "roofing",
        "Hi {first_name} — the ice accumulation over {city} is heavy enough to form ice "
        "dams along the eaves. When melt water backs up behind that ridge of ice it runs "
        "underneath the shingles rather than off the roof, and it typically shows first as "
        "a stain on an upstairs ceiling. Loaded gutters can also pull fascia away from the "
        "structure. We are checking eave lines, attic insulation gaps, and gutter "
        "attachment for homes in your area. Reply YES and we will schedule an assessment "
        "before the next thaw cycle.",
        0.31,
        640,
    ),
    (
        "tpl_flood_restoration",
        "Flash Flood — 24-Hour Water Extraction Window",
        "Flash Flood Warning",
        "restoration",
        "Hi {first_name} — flash flooding hit {city} and the clock matters more than the "
        "water level. Mould colonises wet drywall and substrate in roughly 24 to 48 hours, "
        "so extraction done today is a fraction of the cost of remediation done next week. "
        "Our crews are staged in your area with truck-mounted extraction and commercial "
        "dehumidifiers. We will pull standing water, set drying equipment, take moisture "
        "readings behind the walls, and document everything for your claim. Reply YES and "
        "we will dispatch.",
        0.38,
        970,
    ),
    (
        "tpl_heat_hvac",
        "Excessive Heat — AC Tune-Up Before Peak Load",
        "Excessive Heat Warning",
        "hvac",
        "Hi {first_name} — {city} is heading into an excessive heat warning and this is "
        "when marginal systems fail. A compressor that has been running slightly low on "
        "refrigerant all summer will hold up fine at 95 degrees and quit at 112, usually "
        "on the hottest afternoon when every HVAC company in the valley is already booked "
        "three days out. A tune-up now checks refrigerant charge, capacitor health, and "
        "coil condition, and it is the difference between an hour of maintenance and an "
        "emergency replacement. We have slots this week. Reply YES to claim one.",
        0.22,
        2_080,
    ),
    (
        "tpl_hurricane_roof",
        "Hurricane Warning — Pre-Landfall Envelope Inspection",
        "Hurricane Warning",
        "roofing",
        "Hi {first_name} — with a hurricane warning posted for {city}, the roof details "
        "that matter are the ones at the edges: ridge caps, drip edge, and any flashing "
        "that has already loosened. Wind uplift starts at a weak point and peels from "
        "there, so a single lifted section can cost you the envelope. We are doing rapid "
        "pre-landfall inspections and emergency fastening for homes in the warning area, "
        "and we will put you at the front of the post-storm queue for a full damage "
        "assessment once it is safe. Reply YES and we will get a crew to you.",
        0.47,
        410,
    ),
]


# =====================================================================
# WRITERS -- all UPSERT, so re-running is safe.
# =====================================================================
def _seed_event_service_map(cur) -> int:
    execute_values(
        cur,
        """
        INSERT INTO event_service_map
            (event_type, service_type, urgency_weight, damage_mode)
        VALUES %s
        ON CONFLICT (event_type, service_type) DO UPDATE SET
            urgency_weight = EXCLUDED.urgency_weight,
            damage_mode    = EXCLUDED.damage_mode
        """,
        EVENT_SERVICE_MAP,
    )
    return len(EVENT_SERVICE_MAP)


def _seed_customers(cur) -> int:
    rows = build_customers()
    execute_values(
        cur,
        """
        INSERT INTO customers
            (customer_id, tenant, name, email, phone, city, state, zip, lat, lon,
             service_type, contract_value, lifetime_value, est_job_value,
             tenure_start, tier, assigned_rep, status, is_prospect)
        VALUES %s
        ON CONFLICT (customer_id) DO UPDATE SET
            tenant         = EXCLUDED.tenant,
            name           = EXCLUDED.name,
            email          = EXCLUDED.email,
            phone          = EXCLUDED.phone,
            city           = EXCLUDED.city,
            state          = EXCLUDED.state,
            zip            = EXCLUDED.zip,
            lat            = EXCLUDED.lat,
            lon            = EXCLUDED.lon,
            service_type   = EXCLUDED.service_type,
            contract_value = EXCLUDED.contract_value,
            lifetime_value = EXCLUDED.lifetime_value,
            est_job_value  = EXCLUDED.est_job_value,
            tenure_start   = EXCLUDED.tenure_start,
            tier           = EXCLUDED.tier,
            assigned_rep   = EXCLUDED.assigned_rep,
            status         = EXCLUDED.status,
            is_prospect    = EXCLUDED.is_prospect
        """,
        rows,
    )
    return len(rows)


def _seed_templates(cur) -> int:
    rows = [
        (t_id, name, event, service, msg, rate, sends)
        for (t_id, name, event, service, msg, rate, sends) in OUTREACH_TEMPLATES
    ]
    execute_values(
        cur,
        """
        INSERT INTO outreach_templates
            (template_id, campaign_name, event_type, service_type,
             message_text, past_booked_rate, sends)
        VALUES %s
        ON CONFLICT (template_id) DO UPDATE SET
            campaign_name    = EXCLUDED.campaign_name,
            event_type       = EXCLUDED.event_type,
            service_type     = EXCLUDED.service_type,
            message_text     = EXCLUDED.message_text,
            past_booked_rate = EXCLUDED.past_booked_rate,
            sends            = EXCLUDED.sends
        """,
        rows,
    )
    return len(rows)


def run() -> None:
    with lakebase.cursor() as cur:
        n_map = _seed_event_service_map(cur)
        n_cust = _seed_customers(cur)
        n_tpl = _seed_templates(cur)

    print(f"  event_service_map    {n_map:>4} rows")
    print(f"  customers            {n_cust:>4} rows")
    print(f"  outreach_templates   {n_tpl:>4} rows")
    print("Seed complete.")


if __name__ == "__main__":
    lakebase.ensure_schema()
    run()
