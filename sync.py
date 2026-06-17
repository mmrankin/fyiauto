"""ETL: upstream SQL Servers -> local SQLite serving database.

Pipeline per batch of VINs pulled newest-first from tbl_inventory (the whole
table is the live/in-stock set):

  1. Pull display fields straight from tbl_inventory  (fast, indexed).
  2. Decode year/make/model/trim/body/drivetrain/MPG from VIN_Data1 by exact
     VIN match on the decode server (10.1.1.10).
  3. Squish-VIN propagation: any VIN we couldn't decode inherits YMM from a
     decoded sibling sharing its squish pattern (VIN[0:8]+VIN[9:11]). This is
     the Squish-VIN trick -- one decoded unit covers every same-trim unit.
  4. Photos from tbl_parsedImage (capped), falling back to the photoURLs list
     already carried on the inventory row if that table is slow for the batch.
  5. Dealers from tbl_dealer + tbl_NameAddress.

Everything upstream is read WITH (NOLOCK) + OPTION (MAXDOP 1). Batches are kept
small and each upstream call is wrapped so one slow/oversized batch degrades
gracefully instead of aborting the whole sync.

Usage:
    python sync.py            # respects SYNC_LIMIT (dev cap)
    python sync.py --full     # ignore SYNC_LIMIT, sync everything
    python sync.py --limit N
"""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import local_db
import source_db

BATCH = int(os.environ.get("SYNC_BATCH", "100"))
MAX_PHOTOS = int(os.environ.get("SYNC_MAX_PHOTOS", "20"))
# Photo source:
#   "field"       reads the photoURLs list on the (fast) inventory row.
#   "parsedimage" queries tbl_parsedImage by VIN -- AUTHORITATIVE, but that
#                 table currently holds 2.5B rows with ALL indexes DISABLED,
#                 so every VIN lookup full-scans and times out. Only switch to
#                 this once indexVIN is rebuilt upstream:
#                   ALTER INDEX indexVIN ON dbo.tbl_parsedImage REBUILD;
# Default is the safe path so a sync never stalls on the unindexed table.
PHOTO_SOURCE = os.environ.get("SYNC_PHOTOS", "field").strip().lower()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def squish_vin(vin):
    """11th-gen Squish VIN: positions 1-8 + 10-11 (drop the position-9 check
    digit). Stable across every unit of the same year/make/model/trim."""
    if not vin or len(vin) < 11:
        return None
    return (vin[:8] + vin[9:11]).upper()


def condition_of(new_used, certified):
    cert = (certified or "").strip().lower()
    if cert and cert not in ("no", "n", "0", "false"):
        return "certified"
    return "new" if (new_used or "").upper().startswith("N") else "used"


def _clean(s):
    return s.strip() if isinstance(s, str) else s


def _quote_in(values):
    """Build a safe IN-list of VINs (alnum only -> no escaping needed)."""
    safe = ["'%s'" % v for v in values if v and v.replace("-", "").isalnum()]
    return ",".join(safe)


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------
# Upstream fetches
# --------------------------------------------------------------------------

INV_COLS = """vin, dealerID, stockNumber, newUsed, certified, MSRP, price, mileage,
    trimDescription, transmission, engine, body, fuelType, exteriorColor, interiorColor,
    doors, options, features, description, photoURLs, vehicleURL, lotDate"""


def fetch_inventory(cur, limit, dealer=None):
    top = f"TOP {limit} " if limit else ""
    where = "WHERE dealerID = %d " % int(dealer) if dealer else ""
    cur.execute(
        f"SELECT {top}{INV_COLS} FROM dbo.tbl_inventory WITH (NOLOCK) "
        f"{where}ORDER BY lotDate DESC OPTION (MAXDOP 1)"
    )
    return cur.fetchall()


def iter_inventory(cur, limit, dealer, page_size):
    """Yield inventory in batches.

    Limited runs take the newest `limit` rows by lotDate (small, one fetch).
    A full run (limit=None) streams the whole table in keyset pages by VIN
    (the clustered key) so 2.8M rows never load into memory at once.
    """
    if limit:
        rows = fetch_inventory(cur, limit, dealer)
        for i in range(0, len(rows), page_size):
            yield rows[i:i + page_size]
        return

    dealer_clause = "AND dealerID = %d " % int(dealer) if dealer else ""
    last = ""
    while True:
        cur.execute(
            f"SELECT TOP {page_size} {INV_COLS} FROM dbo.tbl_inventory WITH (NOLOCK) "
            f"WHERE vin > %s {dealer_clause}ORDER BY vin OPTION (MAXDOP 1)",
            (last,),
        )
        rows = cur.fetchall()
        if not rows:
            return
        yield rows
        last = rows[-1]["vin"]
        if len(rows) < page_size:
            return


def inventory_count(cur, dealer=None):
    """Fast row count of tbl_inventory (whole table) via metadata, for progress."""
    if dealer:
        return None
    try:
        cur.execute(
            "SELECT SUM(p.rows) n FROM sys.partitions p "
            "WHERE p.object_id = OBJECT_ID('dbo.tbl_inventory') AND p.index_id IN (0,1)"
        )
        return cur.fetchone()["n"]
    except Exception:
        return None


def fetch_decode(dec_cur, vins):
    """vin -> decode row, by exact VIN match in VIN_Data1."""
    out = {}
    inlist = _quote_in(vins)
    if not inlist:
        return out
    try:
        dec_cur.execute(
            "SELECT VIN, year, make, model, trim, body, drive_type, doors, "
            "MPG_City, MPG_Highway, price_msrp FROM dbo.VIN_Data1 WITH (NOLOCK) "
            f"WHERE VIN IN ({inlist}) OPTION (MAXDOP 1)"
        )
        for r in dec_cur.fetchall():
            out[r["VIN"]] = r
    except Exception as e:  # noqa: BLE001 - tolerate a slow/oversized batch
        print(f"  ! decode batch failed ({len(vins)} vins): {str(e)[:80]}")
    return out


PHOTO_CHUNK = int(os.environ.get("SYNC_PHOTO_CHUNK", "400"))


def fetch_photos(inv_cur, vins):
    """vin -> ordered [url, ...] from tbl_parsedImage.

    One batched IN-list query per PHOTO_CHUNK VINs, capped to MAX_PHOTOS each via
    a ROW_NUMBER window. (This batched form used to time out while the table's
    VIN indexes were disabled; with ix_vin rebuilt it returns hundreds of VINs
    per query, far faster than per-VIN seeks.)
    """
    out = {}
    valid = [v for v in vins if v and v.replace("-", "").isalnum()]
    for chunk in chunked(valid, PHOTO_CHUNK):
        inq = ",".join("'%s'" % v for v in chunk)
        try:
            inv_cur.execute(
                "SELECT VIN, remoteURL FROM ("
                "  SELECT VIN, remoteURL, "
                "  ROW_NUMBER() OVER (PARTITION BY VIN ORDER BY id) rn "
                f"  FROM dbo.tbl_parsedImage WITH (NOLOCK) "
                f"  WHERE VIN IN ({inq}) AND remoteURL IS NOT NULL"
                f") t WHERE rn <= {MAX_PHOTOS} OPTION (MAXDOP 1)"
            )
            for r in inv_cur.fetchall():
                url = _clean(r["remoteURL"])
                if url:
                    out.setdefault(r["VIN"], []).append(url)
        except Exception as e:  # noqa: BLE001
            print(f"  ! photo batch failed ({len(chunk)} vins): {str(e)[:70]}")
    return out


def photos_from_inventory_field(photo_urls):
    """Fallback: split the delimited photoURLs string carried on inventory."""
    if not photo_urls:
        return []
    for sep in ("|", ",", ";", "\n"):
        if sep in photo_urls:
            return [u.strip() for u in photo_urls.split(sep) if u.strip()][:MAX_PHOTOS]
    return [photo_urls.strip()]


DEALER_COLS = (
    "d.id, d.franchise, na.businessName, na.address1, na.address2, "
    "na.city, na.state, na.postalCode, na.latitude, na.longitude"
)


def load_zip_geo(conn):
    """Pull the US ZIP -> lat/long centroids from ip2Location into zip_geo, then
    geocode any dealer whose own coordinates are missing/zero by its ZIP."""
    ip = source_db.connect_ip2location()
    try:
        cur = ip.cursor()
        cur.execute(
            "SELECT zip_code, MIN(latitude) lat, MIN(longitude) lng, "
            "MIN(city_name) city, MIN(region_name) region "
            "FROM dbo.ip2location WITH (NOLOCK) "
            "WHERE country_code = 'US' AND zip_code IS NOT NULL AND zip_code <> '' "
            "AND latitude <> 0 GROUP BY zip_code OPTION (MAXDOP 1)"
        )
        rows = [(r["zip_code"], r["lat"], r["lng"], _clean(r["city"]), _clean(r["region"]))
                for r in cur.fetchall()]
    finally:
        ip.close()

    conn.executemany(
        "INSERT INTO zip_geo (zip_code, latitude, longitude, city, region) "
        "VALUES (?,?,?,?,?) ON CONFLICT(zip_code) DO UPDATE SET "
        "latitude=excluded.latitude, longitude=excluded.longitude, "
        "city=excluded.city, region=excluded.region",
        rows,
    )
    # Geocode dealers from their ZIP (first 5 digits) where coords are absent.
    conn.execute(
        "UPDATE dealers SET "
        "latitude = (SELECT latitude FROM zip_geo WHERE zip_code = substr(dealers.postal_code,1,5)), "
        "longitude = (SELECT longitude FROM zip_geo WHERE zip_code = substr(dealers.postal_code,1,5)) "
        "WHERE (latitude IS NULL OR latitude = 0) "
        "AND EXISTS (SELECT 1 FROM zip_geo WHERE zip_code = substr(dealers.postal_code,1,5))"
    )
    conn.commit()
    return len(rows)


def fetch_all_dealers(inv_cur):
    """Every dealer in tbl_dealer joined to its tbl_NameAddress record. The full
    base (~68k) so Dealer Search by ZIP/city/name works regardless of which
    vehicles are synced."""
    inv_cur.execute(
        f"SELECT {DEALER_COLS} FROM dbo.tbl_dealer d WITH (NOLOCK) "
        "LEFT JOIN dbo.tbl_NameAddress na WITH (NOLOCK) ON na.id = d.nameAddressID "
        "OPTION (MAXDOP 1)"
    )
    return inv_cur.fetchall()


# --------------------------------------------------------------------------
# Transform + load
# --------------------------------------------------------------------------

def build_vehicle(inv, decode, squish_map, photos):
    vin = inv["vin"]
    sq = squish_vin(vin)
    d = decode.get(vin)
    source = "vin_exact" if d else None
    if d is None and sq in squish_map:
        d = squish_map[sq]
        source = "squish"

    def dv(key, fallback=None):
        return (d[key] if d and d.get(key) not in (None, "") else fallback)

    if not source:
        source = "parsed" if (inv["trimDescription"] or inv["body"]) else "none"

    return {
        "vin": vin,
        "dealer_id": inv["dealerID"],
        "stock_number": _clean(inv["stockNumber"]),
        "condition": condition_of(inv["newUsed"], inv["certified"]),
        "price": inv["price"] or 0,
        "msrp": inv["MSRP"] or dv("price_msrp") or 0,
        "mileage": inv["mileage"] or 0,
        "year": dv("year"),
        "make": _clean(dv("make")),
        "model": _clean(dv("model")),
        "trim": _clean(dv("trim")) or _clean(inv["trimDescription"]),
        "body": _clean(dv("body")) or _clean(inv["body"]),
        "drivetrain": _clean(dv("drive_type")),
        "transmission": _clean(inv["transmission"]),
        "engine": _clean(inv["engine"]),
        "fuel_type": _clean(inv["fuelType"]),
        "doors": dv("doors") or inv["doors"],
        "ext_color": _clean(inv["exteriorColor"]),
        "int_color": _clean(inv["interiorColor"]),
        "mpg_city": dv("MPG_City"),
        "mpg_highway": dv("MPG_Highway"),
        "squish_vin": sq,
        "decode_source": source,
        "options": _clean(inv["options"]),
        "features": _clean(inv["features"]),
        "description": _clean(inv["description"]),
        "vehicle_url": _clean(inv["vehicleURL"]),
        "primary_photo": photos[0] if photos else None,
        "photo_count": len(photos),
        "lot_date": str(inv["lotDate"]) if inv["lotDate"] else None,
    }


VEHICLE_COLS = [
    "vin", "dealer_id", "stock_number", "condition", "price", "msrp", "mileage",
    "year", "make", "model", "trim", "body", "drivetrain", "transmission",
    "engine", "fuel_type", "doors", "ext_color", "int_color", "mpg_city",
    "mpg_highway", "squish_vin", "decode_source", "options", "features",
    "description", "vehicle_url", "primary_photo", "photo_count", "lot_date",
]


def upsert_vehicles(conn, vehicles):
    cols = ", ".join(VEHICLE_COLS)
    ph = ", ".join("?" for _ in VEHICLE_COLS)
    def set_clause(c):
        # Preserve photos already loaded when this run carries none (field mode).
        if c == "primary_photo":
            return "primary_photo=COALESCE(excluded.primary_photo, vehicles.primary_photo)"
        if c == "photo_count":
            return ("photo_count=CASE WHEN excluded.photo_count > 0 "
                    "THEN excluded.photo_count ELSE vehicles.photo_count END")
        return f"{c}=excluded.{c}"

    sql = (
        f"INSERT INTO vehicles ({cols}, synced_at) VALUES ({ph}, datetime('now')) "
        f"ON CONFLICT(vin) DO UPDATE SET "
        + ", ".join(set_clause(c) for c in VEHICLE_COLS if c != "vin")
        + ", synced_at=datetime('now')"
    )
    conn.executemany(sql, [[v[c] for c in VEHICLE_COLS] for v in vehicles])


def upsert_photos(conn, vin, urls):
    conn.execute("DELETE FROM photos WHERE vin = ?", (vin,))
    conn.executemany(
        "INSERT OR REPLACE INTO photos (vin, seq, url) VALUES (?, ?, ?)",
        [(vin, i, u) for i, u in enumerate(urls)],
    )


def upsert_dealers(conn, dealers):
    rows = []
    for r in dealers:
        if not r.get("businessName") and not r.get("city"):
            continue  # skip dealers with no usable address record
        rows.append((
            r["id"], _clean(r.get("businessName")),
            1 if (r.get("franchise") or "").strip() in ("1", "Y", "y", "T") else 0,
            _clean(r.get("address1")), _clean(r.get("address2")),
            _clean(r.get("city")), _clean(r.get("state")),
            _clean(r.get("postalCode")), None,
            r.get("latitude"), r.get("longitude"),
        ))
    conn.executemany(
        "INSERT INTO dealers (id, name, franchise, address1, address2, city, state, "
        "postal_code, phone, latitude, longitude) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, franchise=excluded.franchise, "
        "address1=excluded.address1, address2=excluded.address2, city=excluded.city, "
        "state=excluded.state, postal_code=excluded.postal_code, "
        "latitude=excluded.latitude, longitude=excluded.longitude",
        rows,
    )


def propagate_squish(conn):
    """Global Squish-VIN fill: any vehicle still missing YMM inherits it from a
    decoded sibling sharing its squish pattern, anywhere in the synced set.

    Per-batch propagation only sees one batch at a time; this final pass closes
    the gap so e.g. a decoded Bronco lifts every other same-trim Bronco. This is
    the core payoff of the Squish VIN -- one decoded unit covers the whole trim.
    """
    canon = {}
    for r in conn.execute(
        "SELECT squish_vin, year, make, model, trim, body, drivetrain, doors, "
        "mpg_city, mpg_highway FROM vehicles "
        "WHERE make IS NOT NULL AND squish_vin IS NOT NULL"
    ):
        canon.setdefault(r["squish_vin"], r)

    updates = []
    for m in conn.execute(
        "SELECT vin, squish_vin FROM vehicles WHERE make IS NULL AND squish_vin IS NOT NULL"
    ):
        c = canon.get(m["squish_vin"])
        if c:
            updates.append((
                c["year"], c["make"], c["model"], c["trim"], c["body"],
                c["drivetrain"], c["doors"], c["mpg_city"], c["mpg_highway"], m["vin"],
            ))
    conn.executemany(
        "UPDATE vehicles SET year=?, make=?, model=?, "
        "trim=COALESCE(trim, ?), body=COALESCE(body, ?), drivetrain=?, "
        "doors=COALESCE(doors, ?), mpg_city=?, mpg_highway=?, "
        "decode_source='squish_global' WHERE vin=?",
        updates,
    )
    return len(updates)


def rebuild_fts(conn):
    conn.execute("DELETE FROM vehicles_fts")
    conn.execute(
        "INSERT INTO vehicles_fts (vin, year, make, model, trim, body, ext_color, "
        "int_color, options, features, description) "
        "SELECT vin, CAST(year AS TEXT), make, model, trim, body, ext_color, "
        "int_color, options, features, description FROM vehicles"
    )


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def run(limit, dealer=None):
    local_db.init_db()
    print(f"Connecting to upstream sources... (batch={BATCH}, limit={limit or 'ALL'}"
          f"{', dealer ' + str(dealer) if dealer else ''})")
    inv = source_db.connect_inventory()
    photo = source_db.connect_inventory()   # separate cursor for photo queries
    dec = source_db.connect_decode()
    conn = local_db.connect()
    # The vehicle load runs before we can guarantee every referenced dealer
    # exists upstream; don't let an orphan dealer_id abort the sync.
    conn.execute("PRAGMA foreign_keys = OFF")
    inv_cur = inv.cursor()
    photo_cur = photo.cursor()
    dec_cur = dec.cursor()

    print("Loading dealers (full base)...")
    dealers = fetch_all_dealers(inv_cur)
    upsert_dealers(conn, dealers)
    conn.commit()
    print(f"  {len(dealers)} dealers loaded from tbl_dealer + tbl_NameAddress")

    print("Loading ZIP geocodes + geocoding dealers...")
    zips = load_zip_geo(conn)
    print(f"  {zips} US ZIP centroids loaded from ip2Location")

    grand = inventory_count(inv_cur, dealer) if not limit else limit
    page_size = max(BATCH, 1000) if not limit else BATCH
    total = decoded = photo_hits = 0
    print(f"Loading vehicles (photos from {PHOTO_SOURCE}, target {grand or '?'})...")

    for batch in iter_inventory(inv_cur, limit, dealer, page_size):
        vins = [r["vin"] for r in batch]
        decode = fetch_decode(dec_cur, vins)
        # squish map from this batch's exact decodes (sibling propagation)
        squish_map = {}
        for v, d in decode.items():
            sq = squish_vin(v)
            if sq and sq not in squish_map:
                squish_map[sq] = d

        photos = fetch_photos(photo_cur, vins) if PHOTO_SOURCE == "parsedimage" else {}

        vehicles = []
        for r in batch:
            vin = r["vin"]
            urls = photos.get(vin) or photos_from_inventory_field(r["photoURLs"])
            if urls:
                photo_hits += 1
            v = build_vehicle(r, decode, squish_map, urls)
            if v["make"]:
                decoded += 1
            vehicles.append(v)
            # Only rewrite photos when we actually fetched some, so a fast
            # field-mode bulk sync never wipes images loaded by a prior
            # parsedimage run.
            if urls:
                upsert_photos(conn, vin, urls)

        upsert_vehicles(conn, vehicles)
        conn.commit()
        total += len(vehicles)
        print(f"  loaded {total}/{grand or '?'}  (decoded {decoded}, photos {photo_hits})")

    photo.close()
    print("Propagating decode across Squish-VIN siblings...")
    filled = propagate_squish(conn)
    conn.commit()
    print(f"  filled {filled} more vehicles from decoded siblings")

    print("Rebuilding search index...")
    rebuild_fts(conn)
    conn.execute(
        "INSERT INTO sync_meta (key, value) VALUES ('last_sync', datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.commit()
    conn.close()
    inv.close()
    dec.close()

    print(
        f"\nDone. {total} vehicles, {decoded} decoded "
        f"({decoded*100//max(total,1)}%), {photo_hits} with photos, {len(dealers)} dealers."
    )


def run_photos_only(limit=None):
    """Backfill photos for vehicles already in the local DB (no inventory
    re-pull), using the fast batched tbl_parsedImage query."""
    local_db.init_db()
    inv = source_db.connect_inventory()
    conn = local_db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = inv.cursor()

    q = "SELECT vin FROM vehicles"
    if limit:
        q += f" LIMIT {int(limit)}"
    vins = [r["vin"] for r in conn.execute(q).fetchall()]
    print(f"Backfilling photos for {len(vins)} vehicles (chunk={PHOTO_CHUNK})...")

    done = hits = 0
    for batch in chunked(vins, PHOTO_CHUNK):
        photos = fetch_photos(cur, batch)
        for vin, urls in photos.items():
            upsert_photos(conn, vin, urls)
            conn.execute(
                "UPDATE vehicles SET primary_photo = ?, photo_count = ? WHERE vin = ?",
                (urls[0], len(urls), vin),
            )
            hits += 1
        conn.commit()
        done += len(batch)
        print(f"  {done}/{len(vins)} processed ({hits} with photos)")

    conn.close()
    inv.close()
    print(f"Done. {hits} vehicles now have photos.")


def run_dealers_only():
    """Refresh just the dealer base (fast) without touching inventory."""
    local_db.init_db()
    inv = source_db.connect_inventory()
    conn = local_db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")
    print("Loading dealers (full base)...")
    dealers = fetch_all_dealers(inv.cursor())
    upsert_dealers(conn, dealers)
    conn.commit()
    inv.close()
    print(f"  {len(dealers)} dealers loaded from tbl_dealer + tbl_NameAddress")
    print("Loading ZIP geocodes + geocoding dealers...")
    zips = load_zip_geo(conn)
    conn.close()
    print(f"Done. {len(dealers)} dealers, {zips} ZIP centroids.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="ignore SYNC_LIMIT")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dealer", type=int, default=None,
                    help="only sync this dealer's inventory")
    ap.add_argument("--dealers-only", action="store_true",
                    help="refresh the full dealer base only (no inventory)")
    ap.add_argument("--photos-only", action="store_true",
                    help="backfill photos for already-synced vehicles")
    args = ap.parse_args()
    if args.dealers_only:
        run_dealers_only()
        return
    if args.photos_only:
        run_photos_only(args.limit)
        return
    if args.limit is not None:
        limit = args.limit
    elif args.full:
        limit = None
    else:
        env = os.environ.get("SYNC_LIMIT", "").strip()
        limit = int(env) if env else None
    run(limit, dealer=args.dealer)


if __name__ == "__main__":
    main()
