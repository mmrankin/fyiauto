"""AI search for fyiAuto.

Two capabilities, both optional — the site is fully usable on dropdowns and
keyword search without an API key:

  parse_query(text)  Natural language -> the same structured filters the
                     dropdowns produce, via the Claude API. Falls back to a
                     keyword (FTS) filter when no key is configured.

  rerank(text, rows) Semantic re-ordering of a candidate result page by how
                     well each listing matches the *intent* of the query.
                     Uses Voyage embeddings when VOYAGE_API_KEY is set; else a
                     single Claude call ranks the candidates; else identity.

Configuration: ANTHROPIC_API_KEY, AI_SEARCH_MODEL, VOYAGE_API_KEY (see .env).
"""

import json
import os
import re

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None

import local_db

MODEL = os.environ.get("AI_SEARCH_MODEL", "claude-haiku-4-5-20251001")
_client = None


def is_enabled():
    return bool(anthropic and os.environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    global _client
    if _client is None and is_enabled():
        _client = anthropic.Anthropic()
    return _client


# --------------------------------------------------------------------------
# Natural language -> structured filters
# --------------------------------------------------------------------------

# The schema we ask the model to emit. Mirrors local_db's whitelisted filters,
# so anything it returns is safe to hand straight to the query builder.
_FILTER_SCHEMA = """{
  "filters": {
    "condition": "new|used|certified",
    "make": "string",
    "model": "string",
    "body": "string e.g. SUV, Sedan, Pickup, Coupe",
    "drivetrain": "string e.g. AWD, FWD, RWD, 4X4",
    "fuel_type": "string e.g. Gasoline, Hybrid, Electric, Diesel",
    "ext_color": "string",
    "year_min": "int", "year_max": "int",
    "price_min": "int", "price_max": "int",
    "mileage_max": "int",
    "vin": "full or partial VIN if the user typed one",
    "zip": "5-digit ZIP code if mentioned",
    "city": "city name if the user wants cars in/near a city",
    "dealer_name": "dealership name if the user named one"
  },
  "sort": "newest|price_asc|price_desc|mileage_asc|year_desc|year_asc",
  "keywords": "leftover descriptive words that aren't a structured filter"
}"""

_SYSTEM = (
    "You convert a car shopper's natural-language request into structured "
    "inventory filters. Return ONLY minified JSON matching this shape:\n"
    + _FILTER_SCHEMA
    + "\nOmit any field you are not confident about. Map loose phrasing to the "
    "enum values (e.g. 'cheap'/'under 20k' -> price_max, 'four wheel drive' -> "
    "drivetrain 'AWD' or '4X4', 'truck' -> body 'Pickup', 'electric' -> "
    "fuel_type 'Electric'). Pull location/identity out too: 'near 55448' -> zip, "
    "'in Coon Rapids' -> city, 'at Walser Hyundai' -> dealer_name, a 11-17 char "
    "alphanumeric string -> vin. Put adjectives like 'family', 'reliable', "
    "'sporty' in keywords, not filters. Never invent a make/model the user "
    "didn't imply."
)

# A VIN uses A-Z and 0-9 except I, O, Q. We accept partial VINs (>=8 chars) but
# require both a letter and a digit so plain model words ("Wrangler") don't match.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{8,17}$", re.I)


def _looks_like_vin(token):
    t = token.strip()
    return bool(_VIN_RE.match(t)) and any(c.isdigit() for c in t) and any(c.isalpha() for c in t)


def resolve_query(query):
    """Top-level search-bar resolver. A bare VIN / ZIP / city / dealer name is
    routed deterministically (no model call needed); everything else goes to the
    NL parser. Returns {'filters', 'sort', 'keywords', 'kind'}."""
    q = (query or "").strip()
    if not q:
        return {"filters": {}, "sort": None, "keywords": "", "kind": "empty"}

    if _looks_like_vin(q):
        return {"filters": {"vin": q.upper()}, "sort": None, "keywords": "", "kind": "vin"}

    # Short alphanumeric token (5-7 chars, has a digit and a letter): treat as a
    # partial VIN only if it actually prefixes an in-stock VIN.
    bare = q.replace("-", "")
    if (5 <= len(bare) <= 7 and bare.isalnum()
            and any(c.isdigit() for c in bare) and any(c.isalpha() for c in bare)
            and local_db.vin_prefix_exists(bare)):
        return {"filters": {"vin": bare.upper()}, "sort": None, "keywords": "", "kind": "vin"}

    loc = local_db.location_kind(q)
    if loc:
        return {"filters": {loc[0]: loc[1]}, "sort": None, "keywords": "", "kind": loc[0]}

    parsed = parse_query(q)
    parsed.setdefault("kind", "vehicle")
    return parsed


def parse_query(text):
    """Return {'filters': {...}, 'sort': str|None, 'keywords': str}."""
    text = (text or "").strip()
    if not text:
        return {"filters": {}, "sort": None, "keywords": ""}

    client = _get_client()
    if client is None:
        # No model available: treat the whole query as keyword search.
        return {"filters": {"q": text}, "sort": None, "keywords": text}

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text").strip()
        data = json.loads(_strip_fence(raw))
    except Exception as e:  # noqa: BLE001 - never let AI break search
        print(f"ai_search.parse_query fallback: {str(e)[:100]}")
        return {"filters": {"q": text}, "sort": None, "keywords": text}

    filters = {k: v for k, v in (data.get("filters") or {}).items()
               if v not in (None, "", "any")}
    keywords = (data.get("keywords") or "").strip()
    if keywords:
        # Keep descriptive leftovers as a keyword constraint over the FTS index.
        filters["q"] = keywords
    sort = data.get("sort") if data.get("sort") in dict(local_db.SORT_LABELS) else None
    return {"filters": filters, "sort": sort, "keywords": keywords}


def _strip_fence(s):
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    return s.strip()


# --------------------------------------------------------------------------
# Semantic re-ranking
# --------------------------------------------------------------------------

def rerank(text, rows):
    """Re-order a candidate page by semantic fit. Safe no-op if disabled or on
    any error — the structured results are already correct, this only improves
    ordering for fuzzy/vibe queries."""
    if not rows or len(rows) < 3:
        return rows
    client = _get_client()
    if client is None:
        return rows
    try:
        catalog = [
            {
                "i": i,
                "v": f"{r.get('year') or ''} {r.get('make') or ''} "
                     f"{r.get('model') or ''} {r.get('trim') or ''} "
                     f"{r.get('body') or ''} {r.get('ext_color') or ''} "
                     f"${r.get('price') or 0} {(r.get('description') or '')[:160]}",
            }
            for i, r in enumerate(rows)
        ]
        msg = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=(
                "Rank these vehicles by how well they match the shopper's intent. "
                "Return ONLY a JSON array of the item indices, best first. Include "
                "every index exactly once."
            ),
            messages=[{
                "role": "user",
                "content": f"Query: {text}\nVehicles: {json.dumps(catalog)}",
            }],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text")
        order = json.loads(_strip_fence(raw))
        seen, ranked = set(), []
        for i in order:
            if isinstance(i, int) and 0 <= i < len(rows) and i not in seen:
                seen.add(i)
                ranked.append(rows[i])
        # Append anything the model dropped, preserving original order.
        ranked.extend(rows[i] for i in range(len(rows)) if i not in seen)
        return ranked
    except Exception as e:  # noqa: BLE001
        print(f"ai_search.rerank skipped: {str(e)[:100]}")
        return rows
