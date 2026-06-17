# fyiAuto

Retail vehicle listing site (like cars.com / cargurus) backed by the inventory
warehouse. Searchable two ways: standard left-hand **dropdown filters** and an
**AI search** box that turns natural language into the same structured filters
(plus semantic re-ranking).

## Architecture

```
upstream SQL Server                         local SQLite              web
┌─────────────────────────────┐   sync.py   ┌──────────────┐  app.py  ┌───────┐
│ inventory @ 10.1.2.17        │ ──────────▶ │ fyiauto.db   │ ───────▶ │ SRP   │
│  tbl_inventory (2.8M live)   │             │  vehicles    │          │ VDP   │
│  tbl_parsedImage  (photos)   │             │  photos      │          │ APIs  │
│  tbl_dealer / tbl_NameAddress│             │  dealers     │          └───────┘
│ vin_decode @ 10.1.1.10       │             │  vehicles_fts│
│  VIN_Data1 (YMM/trim decode) │             └──────────────┘
└─────────────────────────────┘
```

Visitors never touch the upstream servers — the site reads only the local DB.
`sync.py` is the only component that connects upstream, always read-only
(`WITH (NOLOCK) + OPTION (MAXDOP 1)`).

### VIN decoding & the Squish VIN

`tbl_inventory` has no broken-out year/make/model (its `data1VehicleID` is 0),
so YMM/trim is resolved from `vin_decode..VIN_Data1` by exact VIN match. Any
vehicle that can't be matched inherits YMM from a decoded **Squish-VIN sibling**
— VIN positions 1–8 + 10–11 (dropping the position-9 check digit), which is
identical across every unit of the same year/make/model/trim. Exact-match
coverage on recent inventory is ~69%; the rest are too-new/exotic trims not yet
in the decode set, and still list with price/color/body/trim from inventory.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in DB passwords (+ optional ANTHROPIC_API_KEY)
python sync.py              # respects SYNC_LIMIT; use --full for all inventory
python app.py               # http://127.0.0.1:5055
```

## AI search

- `ANTHROPIC_API_KEY` unset → AI box does keyword (FTS) search; dropdowns are
  fully functional.
- Key set → natural language is parsed into structured filters by Claude, and
  the result page is semantically re-ranked. Set `VOYAGE_API_KEY` for true
  vector embeddings (otherwise Claude re-ranks the candidate page).

## Known limitation — photos

`tbl_parsedImage` currently holds **2.5 billion rows with all indexes disabled**
(`indexVIN`, `indexDealerID`). Every VIN lookup full-scans and times out, so
photo sync is off by default (`SYNC_PHOTOS=field`) and listings show a
placeholder. To enable photos, rebuild the index upstream, then set
`SYNC_PHOTOS=parsedimage`:

```sql
ALTER INDEX indexVIN ON dbo.tbl_parsedImage REBUILD;
```
