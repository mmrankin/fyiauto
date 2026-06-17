-- fyiAuto local serving database (SQLite).
-- Populated by sync.py from the upstream SQL Servers; the website reads only
-- from here so visitors never touch production.

CREATE TABLE IF NOT EXISTS dealers (
    id           INTEGER PRIMARY KEY,      -- tbl_dealer.id
    name         TEXT,
    franchise    INTEGER DEFAULT 0,        -- 1 = franchise dealer
    address1     TEXT,
    address2     TEXT,
    city         TEXT,
    state        TEXT,
    postal_code  TEXT,
    phone        TEXT,
    latitude     REAL,
    longitude    REAL
);

CREATE TABLE IF NOT EXISTS vehicles (
    vin            TEXT PRIMARY KEY,
    dealer_id      INTEGER REFERENCES dealers(id),
    stock_number   TEXT,
    condition      TEXT,                   -- new | used | certified
    price          INTEGER,
    msrp           INTEGER,
    mileage        INTEGER,

    -- Decoded (authoritative) year/make/model/trim from VIN_Data1; falls back
    -- to tbl_parsedVehicle text when the VIN isn't decoded upstream.
    year           INTEGER,
    make           TEXT,
    model          TEXT,
    trim           TEXT,
    body           TEXT,
    drivetrain     TEXT,
    transmission   TEXT,
    engine         TEXT,
    fuel_type      TEXT,
    doors          INTEGER,
    ext_color      TEXT,
    int_color      TEXT,
    mpg_city       INTEGER,
    mpg_highway    INTEGER,

    squish_vin     TEXT,                   -- VIN[0:8] + VIN[9:11], YMM/trim key
    decode_source  TEXT,                   -- vin_exact | vehicle_id | parsed | none

    options        TEXT,
    features       TEXT,
    description    TEXT,

    vehicle_url    TEXT,
    primary_photo  TEXT,
    photo_count    INTEGER DEFAULT 0,
    lot_date       TEXT,
    synced_at      TEXT
);

CREATE INDEX IF NOT EXISTS ix_vehicles_make_model ON vehicles(make, model);
CREATE INDEX IF NOT EXISTS ix_vehicles_year       ON vehicles(year);
CREATE INDEX IF NOT EXISTS ix_vehicles_price      ON vehicles(price);
CREATE INDEX IF NOT EXISTS ix_vehicles_condition  ON vehicles(condition);
CREATE INDEX IF NOT EXISTS ix_vehicles_body       ON vehicles(body);
CREATE INDEX IF NOT EXISTS ix_vehicles_dealer     ON vehicles(dealer_id);
CREATE INDEX IF NOT EXISTS ix_vehicles_squish     ON vehicles(squish_vin);
CREATE INDEX IF NOT EXISTS ix_vehicles_lot_date   ON vehicles(lot_date);
CREATE INDEX IF NOT EXISTS ix_vehicles_drivetrain ON vehicles(drivetrain);
CREATE INDEX IF NOT EXISTS ix_vehicles_fuel_type  ON vehicles(fuel_type);
CREATE INDEX IF NOT EXISTS ix_vehicles_ext_color  ON vehicles(ext_color);
CREATE INDEX IF NOT EXISTS ix_vehicles_mileage    ON vehicles(mileage);

CREATE TABLE IF NOT EXISTS photos (
    vin     TEXT REFERENCES vehicles(vin),
    seq     INTEGER,
    url     TEXT,
    width   INTEGER,
    height  INTEGER,
    PRIMARY KEY (vin, seq)
);

-- Full-text index powering keyword search and the semantic-rerank candidate
-- pool. Rebuilt by sync.py after each load.
CREATE VIRTUAL TABLE IF NOT EXISTS vehicles_fts USING fts5(
    vin UNINDEXED,
    year, make, model, trim, body, ext_color, int_color,
    options, features, description,
    tokenize = 'porter unicode61'
);

-- Optional vector store for Voyage embeddings (semantic search). Empty unless
-- VOYAGE_API_KEY is configured at sync time.
CREATE TABLE IF NOT EXISTS embeddings (
    vin    TEXT PRIMARY KEY REFERENCES vehicles(vin),
    dim    INTEGER,
    vec    BLOB
);

-- ZIP -> lat/long centroid (from ip2Location.dbo.ip2location). Geocodes both
-- the dealer ZIPs (their tbl_NameAddress coords are 0) and the ZIP a shopper
-- types, so Dealer Search can rank by real distance.
CREATE TABLE IF NOT EXISTS zip_geo (
    zip_code   TEXT PRIMARY KEY,
    latitude   REAL,
    longitude  REAL,
    city       TEXT,
    region     TEXT
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
