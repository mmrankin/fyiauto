"""Cross-product CTA links for the VDP.

Each vehicle gets three links into the dealer platform's customer-facing apps:

    Check Availability  -> lead form     lead.dlrpro.com/d/<dealer>
    Value My Trade      -> trade-in       trade.dlrpro.com/t/<dealer>
    Estimate Credit     -> credit est.    credit.dlrpro.com/c/<dealer>

A dealer is "enrolled" in a product when the shared platform DB has an active
grant for that product_code. Enrolled dealers link to their own form; everyone
else falls back to the DEMO dealer. The vehicle's year/make/model (and primary
image as IMG) ride along as query params so the form opens pre-filled.

Matching convention: the platform's business `dealer_id` equals the fyiAuto
`tbl_dealer.id` (as a string).

Config (env, see .env.example):
    PLATFORM_DB_PATH        path to the shared platform.db (enrollment source)
    LEAD_FORM_BASE_URL / TRADE_IN_BASE_URL / CREDIT_EST_BASE_URL
"""

import datetime
import os
import sqlite3
from urllib.parse import urlencode

PLATFORM_DB_PATH = os.environ.get(
    "PLATFORM_DB_PATH", "/Users/markrankin/claude/platform/platform.db"
)

# (key, button label, platform product_code, base URL, path) per product.
PRODUCTS = [
    ("lead", "Check Availability", "LEAD_FORM",
     os.environ.get("LEAD_FORM_BASE_URL", "https://lead.dlrpro.com"), "/d/"),
    ("trade", "Value My Trade", "TRADE_IN",
     os.environ.get("TRADE_IN_BASE_URL", "https://trade.dlrpro.com"), "/t/"),
    ("credit", "Estimate Credit", "CREDIT_EST",
     os.environ.get("CREDIT_EST_BASE_URL", "https://credit.dlrpro.com"), "/c/"),
]

DEMO_DEALER = "DEMO"


def is_enrolled(dealer_id, product_code):
    """True if this dealer has an active grant for product_code in the platform DB."""
    if dealer_id in (None, ""):
        return False
    try:
        con = sqlite3.connect(PLATFORM_DB_PATH)
        try:
            today = datetime.date.today().isoformat()
            row = con.execute(
                "SELECT 1 FROM dealer_products "
                "WHERE dealer_id = ? AND product_code = ? "
                "AND (valid_from IS NULL OR valid_from <= ?) "
                "AND (valid_to IS NULL OR valid_to >= ?) LIMIT 1",
                (str(dealer_id), product_code, today, today),
            ).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception:
        return False


def product_links(dealer_id, year=None, make=None, model=None, image=None):
    """The three VDP CTA links, each enrolled-dealer-or-DEMO, vehicle prefilled.
    Returns [{key, label, url}, ...] in lead/trade/credit order."""
    qs = urlencode([(k, v) for k, v in
                    (("year", year), ("make", make), ("model", model), ("IMG", image))
                    if v])
    links = []
    for key, label, code, base, path in PRODUCTS:
        dealer = str(dealer_id) if (dealer_id and is_enrolled(dealer_id, code)) else DEMO_DEALER
        url = base.rstrip("/") + path + dealer + (("?" + qs) if qs else "")
        links.append({"key": key, "label": label, "url": url})
    return links
