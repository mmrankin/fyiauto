"""SEO helpers for fyiAuto — titles, meta descriptions, canonical URLs, Open Graph,
JSON-LD structured data, and the sitemap building blocks.

Centralizes everything search-engine-facing so the routes stay small and the
base template just renders a `seo` dict. Goal: maximize indexable, high-quality
pages (every vehicle + curated make/model/category landing pages) with rich
structured data so listings can win Google vehicle/rich results and Images.
"""
import json
import os
from urllib.parse import urlencode

# Public origin (override per environment). Used for canonical + sitemap URLs.
BASE_URL = os.environ.get("SITE_BASE_URL", "https://fyiauto.com").rstrip("/")
SITE_NAME = "fyiAuto"
DEFAULT_OG_IMAGE = BASE_URL + "/static/og-default.png"

SITEMAP_SHARD = 50000   # rowids per vehicle-sitemap shard (≤50k URLs per file)

# Facet params that define a canonical, indexable landing page (stable order).
# Volatile params (page/sort/per_page/q + numeric ranges) are dropped from the
# canonical so faceted noise doesn't fracture link equity or duplicate content.
CANONICAL_FACETS = ("condition", "category", "make", "model", "trim",
                    "body", "drivetrain", "fuel_type", "dealer_id")


def abs_url(path):
    return BASE_URL + (path if path.startswith("/") else "/" + path)


def _clean(s):
    return " ".join(str(s).split()) if s else ""


def _vehicle_name(v):
    parts = [v.get("year"), v.get("make"), v.get("model"), v.get("trim")]
    return _clean(" ".join(str(p) for p in parts if p))


def canonical_srp(filters):
    """Stable canonical URL for a search page from its meaningful facets only."""
    q = {k: filters[k] for k in CANONICAL_FACETS if filters.get(k)}
    qs = urlencode(q)
    return abs_url("/" + ("?" + qs if qs else ""))


# ---------- Organization / WebSite (site-wide) ----------

def site_jsonld():
    org = {
        "@context": "https://schema.org", "@type": "Organization",
        "name": SITE_NAME, "url": BASE_URL + "/", "logo": DEFAULT_OG_IMAGE,
    }
    website = {
        "@context": "https://schema.org", "@type": "WebSite",
        "name": SITE_NAME, "url": BASE_URL + "/",
        "potentialAction": {
            "@type": "SearchAction",
            "target": {"@type": "EntryPoint",
                       "urlTemplate": BASE_URL + "/?q={search_term_string}"},
            "query-input": "required name=search_term_string",
        },
    }
    return json.dumps([org, website], separators=(",", ":"))


def _breadcrumb(items):
    return {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name,
             **({"item": abs_url(path)} if path else {})}
            for i, (name, path) in enumerate(items)
        ],
    }


# ---------- Vehicle detail page ----------

_COND = {"new": "https://schema.org/NewCondition",
         "used": "https://schema.org/UsedCondition",
         "certified": "https://schema.org/UsedCondition"}


def vehicle_seo(v):
    name = _vehicle_name(v) or "Vehicle"
    vin = v.get("vin")
    canonical = abs_url("/vehicle/%s" % vin)
    dealer = v.get("dealer") or {}
    loc = ", ".join(p for p in (dealer.get("city"), dealer.get("state")) if p)
    price = v.get("price") or 0
    price_txt = "$%s" % format(int(price), ",") if price and int(price) > 1 else "Call for price"
    miles = v.get("mileage") or 0

    title = "%s%s | %s" % (name, (" – " + price_txt) if price_txt else "", SITE_NAME)
    desc_bits = [name]
    if miles and int(miles) > 0:
        desc_bits.append("%s mi" % format(int(miles), ","))
    if price_txt:
        desc_bits.append(price_txt)
    desc = ". ".join([", ".join(desc_bits)])
    extra = [x for x in (v.get("ext_color"), v.get("body"), v.get("drivetrain"),
                         v.get("fuel_type")) if x]
    if extra:
        desc += ". " + ", ".join(extra)
    if loc:
        desc += " for sale in %s" % loc
    desc += ". View photos, specs, pricing & dealer info on %s." % SITE_NAME

    images = []
    if v.get("primary_photo"):
        images.append(v["primary_photo"])
    for p in (v.get("photos") or [])[:12]:
        if p.get("url") and p["url"] not in images:
            images.append(p["url"])

    car = {
        "@context": "https://schema.org", "@type": "Car",
        "name": name, "url": canonical,
        "vehicleIdentificationNumber": vin,
        "brand": {"@type": "Brand", "name": v.get("make")} if v.get("make") else None,
        "manufacturer": {"@type": "Organization", "name": v.get("make")} if v.get("make") else None,
        "model": v.get("model") or None,
        "vehicleModelDate": str(v.get("year")) if v.get("year") else None,
        "bodyType": v.get("body") or None,
        "color": v.get("ext_color") or None,
        "vehicleTransmission": v.get("transmission") or None,
        "fuelType": v.get("fuel_type") or None,
        "driveWheelConfiguration": v.get("drivetrain") or None,
        "numberOfDoors": v.get("doors") or None,
        "image": images or None,
        "description": _clean(v.get("description"))[:500] or None,
    }
    if miles and int(miles) > 0:
        car["mileageFromOdometer"] = {"@type": "QuantitativeValue",
                                      "value": int(miles), "unitCode": "SMI"}
    if v.get("vehicleEngine") or v.get("engine"):
        car["vehicleEngine"] = {"@type": "EngineSpecification", "name": v.get("engine")}
    # Offer
    if price and int(price) > 1:
        seller = None
        if dealer.get("name"):
            seller = {"@type": "AutoDealer", "name": dealer["name"]}
            addr = {k2: dealer.get(k1) for k1, k2 in (
                ("address1", "streetAddress"), ("city", "addressLocality"),
                ("state", "addressRegion"), ("postal_code", "postalCode")) if dealer.get(k1)}
            if addr:
                addr["@type"] = "PostalAddress"
                addr["addressCountry"] = "US"
                seller["address"] = addr
            if dealer.get("phone"):
                seller["telephone"] = dealer["phone"]
        car["offers"] = {
            "@type": "Offer", "url": canonical, "priceCurrency": "USD",
            "price": int(price), "availability": "https://schema.org/InStock",
            "itemCondition": _COND.get((v.get("condition") or "used").lower(),
                                       "https://schema.org/UsedCondition"),
            **({"seller": seller} if seller else {}),
        }
    car = {k: val for k, val in car.items() if val is not None}

    crumbs = [("Home", "/")]
    if v.get("make"):
        crumbs.append((v["make"], "/?" + urlencode({"make": v["make"]})))
        if v.get("model"):
            crumbs.append((v["model"], "/?" + urlencode({"make": v["make"], "model": v["model"]})))
    crumbs.append((name, None))

    return {
        "title": title[:120], "description": desc[:300], "canonical": canonical,
        "og_type": "product", "og_image": v.get("primary_photo") or DEFAULT_OG_IMAGE,
        "robots": "index,follow",
        "jsonld": [json.dumps(car, separators=(",", ":")),
                   json.dumps(_breadcrumb(crumbs), separators=(",", ":"))],
    }


# ---------- Search results / landing pages ----------

def srp_seo(filters, stats, results):
    total = (results or {}).get("total", 0)
    make = filters.get("make")
    model = filters.get("model")
    cond = filters.get("condition")
    cat = filters.get("category")
    body = filters.get("body")

    # Human label for the listing set.
    if make and model:
        label = "%s %s" % (make, model)
    elif make:
        label = make
    elif cat:
        label = cat.upper() if len(cat) <= 3 else cat.title() + "s"
    elif body:
        label = body + "s"
    else:
        label = None

    cond_word = {"new": "New", "used": "Used", "certified": "Certified"}.get(
        (cond or "").lower(), "")

    if label:
        h = " ".join(x for x in (cond_word, label) if x)
        title = "%s for Sale — Browse Listings & Prices | %s" % (h, SITE_NAME)
        desc = ("Shop %s %s for sale on %s. Compare %s+ listings — prices, "
                "mileage, photos & specs from dealers nationwide." %
                (cond_word.lower() or "new & used", label, SITE_NAME,
                 format(total, ",") if total else "thousands of"))
    else:
        title = "Used & New Cars for Sale — Search %s+ Listings | %s" % (
            format((stats or {}).get("vehicles", 0), ","), SITE_NAME)
        desc = ("Search %s used & new vehicles from %s dealers nationwide. "
                "Filter by make, model, price, mileage & more, with AI search, "
                "photos & full specs on %s." % (
                    format((stats or {}).get("vehicles", 0), ","),
                    format((stats or {}).get("dealers", 0), ","), SITE_NAME))

    # Don't index empty/near-empty result sets (thin content / crawl waste).
    robots = "index,follow" if total >= 1 else "noindex,follow"

    items = []
    for i, r in enumerate((results or {}).get("results", [])[:25]):
        if r.get("vin"):
            items.append({"@type": "ListItem", "position": i + 1,
                          "url": abs_url("/vehicle/%s" % r["vin"])})
    jsonld = []
    if items:
        jsonld.append(json.dumps({
            "@context": "https://schema.org", "@type": "ItemList",
            "itemListElement": items}, separators=(",", ":")))
    crumbs = [("Home", "/")]
    if make:
        crumbs.append((make, "/?" + urlencode({"make": make})))
        if model:
            crumbs.append((model, None))
    if len(crumbs) > 1:
        jsonld.append(json.dumps(_breadcrumb(crumbs), separators=(",", ":")))

    return {"title": title[:120], "description": desc[:300],
            "canonical": canonical_srp(filters), "og_type": "website",
            "og_image": DEFAULT_OG_IMAGE, "robots": robots, "jsonld": jsonld}


def simple_seo(title, description, path, robots="index,follow"):
    return {"title": "%s | %s" % (title, SITE_NAME) if SITE_NAME not in title else title,
            "description": description, "canonical": abs_url(path),
            "og_type": "website", "og_image": DEFAULT_OG_IMAGE,
            "robots": robots, "jsonld": []}
