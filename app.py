"""fyiAuto — retail vehicle listing site.

Routes
    GET  /                     Search results page (SRP): left dropdown filters,
                               AI search box, results grid, pagination.
    GET  /vehicle/<vin>        Vehicle detail page (VDP): gallery, specs,
                               options/description, dealer block.
    GET  /api/facets           Facet lists for the dropdowns (narrow as you drill).
    GET  /api/search           JSON search results (same filters as the SRP).
    POST /api/ai-search        Natural-language query -> structured filters
                               (+ semantic re-ranking when available).

Reads only from the local SQLite store populated by sync.py.
"""

import os
import threading

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from flask import Flask, abort, jsonify, render_template, request, url_for

import ai_search
import integrations
import local_db
import vdp_enrich

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")

PER_PAGE = 20
PER_PAGE_OPTIONS = [20, 40, 60, 80]


def _per_page(args):
    try:
        n = int(args.get("per_page", PER_PAGE))
    except (TypeError, ValueError):
        return PER_PAGE
    return n if n in PER_PAGE_OPTIONS else PER_PAGE

# Filter keys accepted from the query string, mapped straight into local_db.
FILTER_KEYS = (
    "condition", "make", "model", "trim", "body", "drivetrain", "fuel_type",
    "ext_color", "doors", "dealer_id", "category",
    "year_min", "year_max", "price_min", "price_max", "mileage_max",
    "vin", "zip", "city", "dealer_name",
)

# "Shop by" quick-pick dropdown: label -> SRP query string.
QUICK_PICKS = [
    ("Cars under $15k", "price_max=15000&sort=price_asc"),
    ("SUVs", "category=suv"),
    ("Pickups", "category=pickup"),
    ("Sedans", "category=sedan"),
    ("Convertibles", "category=convertible"),
    ("Coupes", "category=coupe"),
]


SELL_MY_CAR_URL = os.environ.get("SELL_MY_CAR_URL", "http://localhost:5001/t/DEMO")


@app.context_processor
def _nav_context():
    """Data shown in the header/sub-nav on every rendered page."""
    return {
        "quick_picks": QUICK_PICKS,
        "top_makes": local_db.top_makes(10),
        "sell_my_car_url": SELL_MY_CAR_URL,
    }


@app.context_processor
def _footer():
    import datetime
    return {"current_year": datetime.date.today().year}


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", stats=local_db.stats())


@app.route("/terms")
def terms():
    return render_template("terms.html", stats=local_db.stats())


@app.context_processor
def _assets():
    """Cache-busting static URLs: append the file's mtime so a CSS/JS change
    forces browsers and Cloudflare to fetch the new version immediately."""
    def asset(filename):
        try:
            v = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            v = 0
        return url_for("static", filename=filename, v=v)
    return {"asset": asset}
INT_KEYS = {"year_min", "year_max", "price_min", "price_max", "mileage_max",
            "doors", "dealer_id"}


@app.before_request
def _ensure_db():
    if not getattr(app, "_ready", False):
        local_db.init_db()
        app._ready = True


def _warm_cache():
    """Precompute facets/bounds for the common entry points so the first real
    visitor hits a warm cache (and the DB pages are pulled into RAM). Runs in a
    daemon thread at startup."""
    import time
    time.sleep(1)
    combos = [{}] + [{"category": c} for c in
                     ("suv", "pickup", "sedan", "convertible", "coupe")]
    for f in combos:
        try:
            local_db.facets(f)
            local_db.price_bounds(f)
            local_db.search(f, per_page=PER_PAGE)
        except Exception:  # noqa: BLE001 - warming is best-effort
            pass


threading.Thread(target=_warm_cache, daemon=True).start()


def _filters_from_args(args):
    filters = {}
    for key in FILTER_KEYS:
        val = (args.get(key) or "").strip()
        if not val or val == "any":
            continue
        if key in INT_KEYS:
            try:
                filters[key] = int(val)
            except ValueError:
                continue
        else:
            filters[key] = val
    q = (args.get("q") or "").strip()
    if q:
        filters["q"] = q
    return filters


@app.route("/")
def srp():
    filters = _filters_from_args(request.args)
    sort = request.args.get("sort") or local_db.DEFAULT_SORT
    page = request.args.get("page", 1)
    per_page = _per_page(request.args)

    results = local_db.search(filters, sort=sort, page=page, per_page=per_page)
    facets = local_db.facets(filters)
    bounds = local_db.price_bounds(
        {k: v for k, v in filters.items() if k not in ("price_min", "price_max")}
    )

    # When viewing one dealer's inventory, surface whose it is.
    dealer = None
    if "dealer_id" in filters:
        dealer = local_db.get_dealer_by_id(filters["dealer_id"])

    return render_template(
        "srp.html",
        results=results,
        facets=facets,
        bounds=bounds,
        filters=filters,
        active=request.args,
        sort=sort,
        sorts=local_db.SORT_LABELS,
        stats=local_db.stats(),
        ai_enabled=ai_search.is_enabled(),
        dealer=dealer,
        per_page=per_page,
        per_page_options=PER_PAGE_OPTIONS,
    )


@app.route("/dealers")
def dealers():
    zipc = (request.args.get("zip") or "").strip()
    make = (request.args.get("make") or "").strip()
    page = request.args.get("page", 1)
    per_page = _per_page(request.args)
    results, zip_scope, total = local_db.search_dealers(
        zip=zipc or None, make=make or None, page=page, per_page=per_page
    )
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    return render_template(
        "dealers.html",
        dealers=results,
        total=total,
        page=page,
        pages=(total + per_page - 1) // per_page,
        per_page=per_page,
        per_page_options=PER_PAGE_OPTIONS,
        zip=zipc,
        make=make,
        zip_scope=zip_scope,
        searched=bool(zipc or make),
        stats=local_db.stats(),
    )


@app.route("/vehicle/<vin>")
def vdp(vin):
    vehicle = local_db.get_vehicle(vin)
    if not vehicle:
        abort(404, description="Vehicle not found.")
    # Synthesize a description when the real one is too short (render-only,
    # memoized, no DB write). Fuel/engine/transmission are filled by the sync.
    if vdp_enrich.needs_description(vehicle):
        vehicle["description"] = vdp_enrich.make_description(vehicle)
    related = local_db.related_vehicles(vin, vehicle.get("dealer_id"), n=4)
    cta_links = integrations.product_links(
        vehicle.get("dealer_id"), vehicle.get("year"),
        vehicle.get("make"), vehicle.get("model"),
        image=vehicle.get("primary_photo"),
    )
    return render_template("vdp.html", v=vehicle, related=related, cta_links=cta_links)


@app.route("/api/facets")
def api_facets():
    filters = _filters_from_args(request.args)
    return jsonify({
        "facets": local_db.facets(filters),
        "bounds": local_db.price_bounds(filters),
    })


@app.route("/api/search")
def api_search():
    filters = _filters_from_args(request.args)
    sort = request.args.get("sort") or local_db.DEFAULT_SORT
    page = request.args.get("page", 1)
    return jsonify(local_db.search(filters, sort=sort, page=page, per_page=PER_PAGE))


@app.route("/api/ai-search", methods=["POST"])
def api_ai_search():
    payload = request.get_json(silent=True) or {}
    query = (payload.get("q") or "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400

    parsed = ai_search.resolve_query(query)
    filters = parsed.get("filters", {})

    results = local_db.search(filters, sort=parsed.get("sort"), page=1,
                              per_page=PER_PAGE)

    # A full VIN that resolves to exactly one car jumps straight to its page.
    redirect = None
    if parsed.get("kind") == "vin" and results["total"] == 1:
        redirect = url_for("vdp", vin=results["results"][0]["vin"])
    elif parsed.get("kind") == "vehicle":
        # Semantic re-rank only makes sense for descriptive vehicle searches.
        results["results"] = ai_search.rerank(query, results["results"])

    return jsonify({
        "query": query,
        "interpreted": parsed,
        "filters": filters,
        "results": results,
        "redirect": redirect,
    })


@app.template_filter("money")
def money(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    return f"${n:,}" if n > 0 else "Call for price"


@app.template_filter("miles")
def miles(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    return f"{n:,} mi" if n > 0 else "—"


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5055)),
            debug=debug, threaded=True)
