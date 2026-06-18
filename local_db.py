"""Local SQLite serving layer for fyiAuto.

The website reads exclusively from here. Provides schema init, the facet lists
that drive the left-hand dropdowns, the filtered/sorted/paginated search the
SRP uses, and the single-vehicle + dealer fetches the VDP needs.
"""

import math
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

DB_PATH = os.environ.get(
    "LOCAL_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fyiauto.db"),
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the site keep reading while the nightly sync writes; busy_timeout
    # makes a read wait briefly rather than error if it hits a write lock.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    with open(SCHEMA_PATH) as fh:
        sql = fh.read()
    conn = connect()
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Filtering
#
# A filters dict maps to a whitelisted WHERE clause. Anything not in the maps
# below is ignored, so filters can be fed straight from request args or from
# the AI query parser without injection risk.
# --------------------------------------------------------------------------

# Exact-match (case-insensitive) columns.
_EXACT = {
    "condition": "condition",
    "make": "make",
    "model": "model",
    "trim": "trim",
    "body": "body",
    "drivetrain": "drivetrain",
    "fuel_type": "fuel_type",
    "ext_color": "ext_color",
    "dealer_id": "dealer_id",
    "doors": "doors",
}

# Range columns: (filter_key) -> (column, operator).
_RANGE = {
    "year_min": ("year", ">="),
    "year_max": ("year", "<="),
    "price_min": ("price", ">="),
    "price_max": ("price", "<="),
    "mileage_max": ("mileage", "<="),
}

_SORTS = {
    "price_asc": "price ASC",
    "price_desc": "price DESC",
    "year_desc": "year DESC, price ASC",
    "year_asc": "year ASC",
    "mileage_asc": "mileage ASC",
    "newest": "lot_date DESC",
}
DEFAULT_SORT = "newest"

# Body-style values are messy upstream, so a "category" quick-pick maps to a set
# of body strings matched case-insensitively (body IN (...)).
CATEGORY_BODIES = {
    "suv": ["SUV", "Sport Utility", "Crossover", "Full-Size"],
    "pickup": ["Pickup", "Truck"],
    "sedan": ["Sedan"],
    "convertible": ["Convertible"],
    "coupe": ["Coupe"],
    "van": ["Van", "Cargo Van", "Passenger Van", "Minivan"],
    "wagon": ["Wagon"],
}

SORT_LABELS = [
    ("newest", "Newest listings"),
    ("price_asc", "Price: low to high"),
    ("price_desc", "Price: high to low"),
    ("mileage_asc", "Mileage: low to high"),
    ("year_desc", "Year: newest"),
    ("year_asc", "Year: oldest"),
]


def _build_where(filters):
    clauses, params = [], []
    for key, col in _EXACT.items():
        val = filters.get(key)
        if val in (None, "", "any"):
            continue
        if key in ("dealer_id", "doors"):
            clauses.append(f"{col} = ?")
            params.append(val)
        else:
            # Plain equality so the column index is used (LOWER() forces a full
            # 2.8M-row scan). Facet dropdowns and the AI parser emit exact
            # DB-cased values, so case matches.
            clauses.append(f"{col} = ?")
            params.append(str(val).strip())

    for key, (col, op) in _RANGE.items():
        val = filters.get(key)
        if val in (None, ""):
            continue
        clauses.append(f"{col} {op} ?")
        params.append(val)

    # Quick-pick category -> a group of body styles.
    cat = (filters.get("category") or "").strip().lower()
    if cat in CATEGORY_BODIES:
        bodies = CATEGORY_BODIES[cat]
        clauses.append("body IN (%s)" % ",".join("?" for _ in bodies))
        params.extend(bodies)

    # Identity / location filters (fed by the search bar).
    vin = (filters.get("vin") or "").strip()
    if vin:
        clauses.append("vin LIKE ?")          # prefix match -> partial VIN ok
        params.append(vin.upper() + "%")

    zipc = (filters.get("zip") or "").strip()
    if zipc:
        clauses.append("dealer_id IN (SELECT id FROM dealers WHERE postal_code LIKE ?)")
        params.append(zipc + "%")

    city = (filters.get("city") or "").strip()
    if city:
        clauses.append("dealer_id IN (SELECT id FROM dealers WHERE LOWER(city) = LOWER(?))")
        params.append(city)

    dealer_name = (filters.get("dealer_name") or "").strip()
    if dealer_name:
        clauses.append("dealer_id IN (SELECT id FROM dealers WHERE name LIKE ?)")
        params.append("%" + dealer_name + "%")

    # Keyword filter: restrict to VINs matching an FTS query.
    q = (filters.get("q") or "").strip()
    if q:
        clauses.append(
            "vin IN (SELECT vin FROM vehicles_fts WHERE vehicles_fts MATCH ?)"
        )
        params.append(_fts_query(q))

    # Restrict to an explicit VIN set (used by semantic rerank).
    vins = filters.get("vins")
    if vins:
        placeholders = ",".join("?" for _ in vins)
        clauses.append(f"vin IN ({placeholders})")
        params.extend(vins)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _count(where, params):
    conn = connect()
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM vehicles{where}", params
        ).fetchone()[0]
    finally:
        conn.close()


def _fts_query(text):
    """Turn free text into a forgiving FTS5 query: prefix-match each token, OR'd."""
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f"{t}*" for t in tokens)


def search(filters=None, sort=None, page=1, per_page=24):
    filters = filters or {}
    where, params = _build_where(filters)
    order = _SORTS.get(sort or DEFAULT_SORT, _SORTS[DEFAULT_SORT])
    page = max(1, int(page))
    offset = (page - 1) * per_page

    # The COUNT over a broad filter scans millions of rows and is identical for
    # every page/sort of a filter set, so cache it per sync version. The page
    # slice itself is fast via the lot_date index.
    total = _cached("count", filters, lambda: _count(where, params))

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM vehicles{where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
    finally:
        conn.close()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "results": [dict(r) for r in rows],
    }


# Facet/bounds results only change when the data does (nightly sync). Cache them
# keyed by the sync version so the heavy GROUP BYs run at most once per sync.
_cache = {}


def _sync_version():
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key = 'last_sync'"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else "0"


def _cached(kind, filters, compute):
    try:
        key = (kind, _sync_version(),
               tuple(sorted((filters or {}).items(), key=lambda kv: kv[0])))
        hash(key)
    except TypeError:               # unhashable filter value -> skip the cache
        return compute()
    if key in _cache:
        return _cache[key]
    if len(_cache) > 2000:          # bound memory; drop oldest-ish wholesale
        _cache.clear()
    value = compute()
    _cache[key] = value
    return value


def facets(filters=None):
    """Distinct values for the dropdowns. Honors the *other* active filters so
    the options narrow as the user drills down (e.g. models for the make).
    Cached per sync version."""
    return _cached("facets", filters, lambda: _facets(filters))


def _facets(filters=None):
    filters = dict(filters or {})

    # High-cardinality columns (raw OEM color strings number in the thousands)
    # are capped to the most common values so the dropdown stays usable.
    TOP_N = {"ext_color": 40, "model": 200}

    def distinct(column, exclude_key):
        sub = {k: v for k, v in filters.items() if k != exclude_key}
        where, params = _build_where(sub)
        joiner = "AND" if where else "WHERE"
        cap = TOP_N.get(column)
        order = "ORDER BY n DESC LIMIT %d" % cap if cap else "ORDER BY %s" % column
        conn = connect()
        try:
            rows = conn.execute(
                f"SELECT {column} AS v, COUNT(*) AS n FROM vehicles{where} "
                f"{joiner} {column} IS NOT NULL AND {column} <> '' "
                f"GROUP BY {column} {order}",
                params,
            ).fetchall()
        finally:
            conn.close()
        if cap:                       # present capped facets alphabetically
            rows = sorted(rows, key=lambda r: str(r["v"]))
        return [{"value": r["v"], "count": r["n"]} for r in rows]

    # Run the eight GROUP BYs concurrently — each uses its own connection and
    # WAL allows parallel reads, so wall time is the slowest single facet rather
    # than the sum. Big win when a broad filter (e.g. all SUVs) is active.
    cols = ["condition", "make", "model", "body",
            "drivetrain", "fuel_type", "ext_color", "year"]
    with ThreadPoolExecutor(max_workers=len(cols)) as ex:
        results = ex.map(lambda c: (c, distinct(c, c)), cols)
    return dict(results)


def price_bounds(filters=None):
    return _cached("bounds", filters, lambda: _price_bounds(filters))


def _price_bounds(filters=None):
    where, params = _build_where(filters or {})
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT MIN(price) AS lo, MAX(price) AS hi FROM vehicles{where} "
            + ("AND price > 0" if where else "WHERE price > 0"),
            params,
        ).fetchone()
    finally:
        conn.close()
    return {"min": row["lo"] or 0, "max": row["hi"] or 0}


def top_makes(n=10):
    """The most common makes in stock (decoded tbl_inventory). Powers the make
    dropdown in Dealer Search."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT make, COUNT(*) AS c FROM vehicles "
            "WHERE make IS NOT NULL AND make <> '' "
            "GROUP BY make ORDER BY c DESC, make LIMIT ?", (n,)
        ).fetchall()
    finally:
        conn.close()
    return [r["make"] for r in rows]


def geocode_zip(zip):
    """(lat, lng) for a ZIP from zip_geo (populated from ip2Location), or None."""
    z = (zip or "").strip()[:5]
    if len(z) < 5:
        return None
    conn = connect()
    try:
        r = conn.execute(
            "SELECT latitude, longitude FROM zip_geo WHERE zip_code = ?", (z,)
        ).fetchone()
    finally:
        conn.close()
    return (r["latitude"], r["longitude"]) if r and r["latitude"] else None


def _haversine(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _dealers_near(lat0, lng0, make, radius_mi, cap=2000):
    """All dealers within radius_mi of a point, nearest first, with distance_mi
    (up to cap). The caller paginates the returned list."""
    dlat = radius_mi / 69.0
    dlng = radius_mi / (69.0 * max(math.cos(math.radians(lat0)), 0.01))

    params = []
    make_count = "0"
    if make:
        make_count = ("(SELECT COUNT(*) FROM vehicles v WHERE v.dealer_id = d.id "
                      "AND v.make = ?)")
        params.append(make)
    params += [lat0, lng0]                         # hav(...)
    params += [lat0 - dlat, lat0 + dlat]           # latitude box
    params += [lng0 - dlng, lng0 + dlng]           # longitude box
    make_clause = ""
    if make:
        make_clause = " AND d.id IN (SELECT dealer_id FROM vehicles WHERE make = ?)"
        params.append(make)
    params.append(cap)

    conn = connect()
    try:
        conn.create_function("hav", 4, _haversine)
        rows = conn.execute(
            f"SELECT d.*, "
            f"(SELECT COUNT(*) FROM vehicles v WHERE v.dealer_id = d.id) AS inventory, "
            f"{make_count} AS make_count, "
            f"hav(?, ?, d.latitude, d.longitude) AS distance_mi "
            f"FROM dealers d "
            f"WHERE d.latitude IS NOT NULL AND d.latitude <> 0 "
            f"AND EXISTS (SELECT 1 FROM vehicles v WHERE v.dealer_id = d.id) "
            f"AND d.latitude BETWEEN ? AND ? AND d.longitude BETWEEN ? AND ?{make_clause} "
            f"ORDER BY distance_mi LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if r["distance_mi"] <= radius_mi:
            d = dict(r)
            d["distance_mi"] = round(d["distance_mi"], 1)
            out.append(d)
    return out


def search_dealers(zip=None, make=None, page=1, per_page=20, radius_mi=150):
    """Paginated dealer search. Returns (dealers_page, scope, total).

    When the ZIP geocodes (via zip_geo / ip2Location) dealers are ranked by true
    distance within radius_mi (each carries distance_mi). Otherwise ZIP falls
    back to exact 5-digit then 3-digit region prefix. scope is
    'distance' | 'exact' | 'region' | None."""
    zip = (zip or "").strip()
    make = (make or "").strip()
    page = max(1, int(page))
    per_page = int(per_page)
    offset = (page - 1) * per_page

    if zip:
        geo = geocode_zip(zip)
        if geo:
            near = _dealers_near(geo[0], geo[1], make, radius_mi)
            if near:
                return near[offset:offset + per_page], "distance", len(near)
            # geocoded but nobody in radius -> fall through to prefix match

    def query(zip_clause, zip_param):
        # Only dealers that actually have stock on the site.
        where = ["EXISTS (SELECT 1 FROM vehicles v WHERE v.dealer_id = d.id)"]
        wparams = []
        if zip_clause:
            where.append(zip_clause)
            wparams.append(zip_param)
        if make:
            where.append("d.id IN (SELECT dealer_id FROM vehicles WHERE make = ?)")
            wparams.append(make)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""

        make_count, mc_params = "0", []
        if make:
            make_count = ("(SELECT COUNT(*) FROM vehicles v WHERE v.dealer_id = d.id "
                          "AND v.make = ?)")
            mc_params = [make]

        conn = connect()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM dealers d{wsql}", wparams
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT d.*, "
                f"(SELECT COUNT(*) FROM vehicles v WHERE v.dealer_id = d.id) AS inventory, "
                f"{make_count} AS make_count, NULL AS distance_mi "
                f"FROM dealers d{wsql} "
                f"ORDER BY make_count DESC, inventory DESC, d.name LIMIT ? OFFSET ?",
                mc_params + wparams + [per_page, offset],
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows], total

    if not zip:
        items, total = query(None, None)
        return items, None, total

    items, total = query("d.postal_code = ?", zip)
    if total or len(zip) < 3:
        return items, "exact", total
    items, total = query("d.postal_code LIKE ?", zip[:3] + "%")
    return items, "region", total


def vin_prefix_exists(prefix):
    """True if any in-stock VIN starts with this (uppercased) prefix. Lets the
    search bar treat a short alphanumeric token as a partial VIN only when it
    actually matches stock, so model names like 'GLE350' aren't misread."""
    p = (prefix or "").strip().upper()
    if not p:
        return False
    conn = connect()
    try:
        return conn.execute(
            "SELECT 1 FROM vehicles WHERE vin LIKE ? LIMIT 1", (p + "%",)
        ).fetchone() is not None
    finally:
        conn.close()


def location_kind(text):
    """Classify a free-text token against the dealer table for the search bar.
    Returns ('city'|'dealer_name'|'zip', value) or None. Used to route a bare
    query like "Coon Rapids" or "Walser Hyundai" to the right filter."""
    t = (text or "").strip()
    if not t:
        return None
    conn = connect()
    try:
        if t.isdigit() and 3 <= len(t) <= 5:
            if conn.execute(
                "SELECT 1 FROM dealers WHERE postal_code LIKE ? LIMIT 1", (t + "%",)
            ).fetchone():
                return ("zip", t)
        if conn.execute(
            "SELECT 1 FROM dealers WHERE LOWER(city) = LOWER(?) LIMIT 1", (t,)
        ).fetchone():
            return ("city", t)
        if len(t) >= 3 and conn.execute(
            "SELECT 1 FROM dealers WHERE name LIKE ? LIMIT 1", ("%" + t + "%",)
        ).fetchone():
            return ("dealer_name", t)
    finally:
        conn.close()
    return None


def get_vehicle(vin):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM vehicles WHERE vin = ?", (vin,)).fetchone()
        if not row:
            return None
        vehicle = dict(row)
        vehicle["photos"] = [
            dict(p)
            for p in conn.execute(
                "SELECT seq, url, width, height FROM photos WHERE vin = ? ORDER BY seq",
                (vin,),
            ).fetchall()
        ]
        vehicle["dealer"] = get_dealer(conn, vehicle.get("dealer_id"))
    finally:
        conn.close()
    return vehicle


def get_dealer(conn, dealer_id):
    if dealer_id is None:
        return None
    row = conn.execute("SELECT * FROM dealers WHERE id = ?", (dealer_id,)).fetchone()
    return dict(row) if row else None


def related_vehicles(vin, dealer_id, n=4):
    """Two groups for the VDP, each ordered photos-first then newest:
      'dealer' - up to n other vehicles from this dealer
      'others' - up to n vehicles from other dealers (the supplement)
    The 'others' group fills more when the dealer has fewer than n of its own,
    so the page always shows a healthy amount of inventory."""
    conn = connect()
    try:
        # Same-dealer subset is small, so photos-first ordering is cheap here.
        same = [dict(r) for r in conn.execute(
            "SELECT * FROM vehicles WHERE dealer_id = ? AND vin <> ? "
            "ORDER BY (photo_count > 0) DESC, lot_date DESC LIMIT ?",
            (dealer_id, vin, n),
        ).fetchall()]

        # 'others' spans every other dealer (~millions of rows): order by the
        # indexed lot_date alone so SQLite uses the index and stops at the limit,
        # but prefer ones with photos via a cheap first pass.
        others_n = max(n - len(same), min(n, 4))
        others = [dict(r) for r in conn.execute(
            "SELECT * FROM vehicles WHERE dealer_id <> ? AND vin <> ? AND photo_count > 0 "
            "ORDER BY lot_date DESC LIMIT ?",
            (dealer_id, vin, others_n),
        ).fetchall()]
        if len(others) < others_n:
            seen = {vin} | {o["vin"] for o in others}
            ph = ",".join("?" for _ in seen)
            others += [dict(r) for r in conn.execute(
                f"SELECT * FROM vehicles WHERE dealer_id <> ? AND vin NOT IN ({ph}) "
                f"ORDER BY lot_date DESC LIMIT ?",
                [dealer_id] + list(seen) + [others_n - len(others)],
            ).fetchall()]
        return {"dealer": same, "others": others}
    finally:
        conn.close()


def get_dealer_by_id(dealer_id):
    conn = connect()
    try:
        return get_dealer(conn, dealer_id)
    finally:
        conn.close()


def stats():
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        d = conn.execute("SELECT COUNT(*) FROM dealers").fetchone()[0]
        last = conn.execute(
            "SELECT value FROM sync_meta WHERE key = 'last_sync'"
        ).fetchone()
    finally:
        conn.close()
    return {"vehicles": n, "dealers": d, "last_sync": last[0] if last else None}
