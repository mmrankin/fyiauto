"""VDP description generation (render-time, no DB writes).

When a vehicle's description is shorter than 20 characters we synthesize one from
its facts. With an ANTHROPIC_API_KEY we ask Claude to phrase it; otherwise we use
a templated blurb. Results are memoized in-process (keyed by VIN) so we don't
recompute / re-call the model on every view. The serving DB is read-only on the
request path — spec enrichment (fuel/engine/transmission) happens in sync.py.
"""

import datetime
import threading

MIN_DESC = 20
_cache = {}
_lock = threading.Lock()


def _money(v):
    try:
        n = int(v)
        return f"${n:,}" if n > 0 else "a great price"
    except (TypeError, ValueError):
        return "a great price"


def _draft(vehicle):
    now = datetime.date.today()
    year = vehicle.get("year")
    age = (now.year - int(year)) if year else None
    age_txt = f"{age} year{'s' if age != 1 else ''}" if age and age > 0 else "no time"
    miles = vehicle.get("mileage") or 0
    miles_txt = f"{int(miles):,} miles" if miles else "very low miles"
    color = (vehicle.get("ext_color") or "").strip()
    dealer = (vehicle.get("dealer") or {}).get("name") or "our dealership"
    lot = (vehicle.get("lot_date") or "")[:10]
    title = " ".join(str(x) for x in (year, vehicle.get("make"), vehicle.get("model")) if x)
    return (
        f"This {color} {title} has only {miles_txt} on it after {age_txt}. "
        f"Offered by {dealer} at {_money(vehicle.get('price'))} and freshly listed "
        f"on {now.isoformat()}{(' (in stock since ' + lot + ')') if lot else ''}, "
        f"it won't last long. Inquire above for financing options or click "
        f'"Check Availability" and we\'ll send you detailed specifications and '
        f"additional photos."
    ).replace("  ", " ").strip()


def _ai_rewrite(draft):
    try:
        import ai_search
        client = ai_search._get_client()
        if client is None:
            return None
        msg = client.messages.create(
            model=ai_search.MODEL,
            max_tokens=220,
            system=(
                "You write short, upbeat used-car listing descriptions for a "
                "dealership website. 2-3 sentences, natural and specific, no hype "
                "words like 'amazing'. Keep the 'Check Availability' call-to-action. "
                "Return only the description text."
            ),
            messages=[{"role": "user", "content":
                       "Rewrite this into a natural listing description, keeping the "
                       "facts accurate:\n\n" + draft}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        return text or None
    except Exception:
        return None


def make_description(vehicle):
    """A description for a vehicle whose own description is too short. Memoized."""
    vin = vehicle.get("vin")
    if vin and vin in _cache:
        return _cache[vin]
    draft = _draft(vehicle)
    text = _ai_rewrite(draft) or draft
    if vin:
        with _lock:
            _cache[vin] = text
    return text


def needs_description(vehicle):
    return len((vehicle.get("description") or "").strip()) < MIN_DESC
