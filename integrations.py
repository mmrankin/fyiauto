"""Cross-product links for the "Check Availability" button on the VDP.

A dealer is "enrolled" when the shared platform database grants them an active
LEAD_FORM product (the Dealer Lead Form app). Enrolled dealers get a button to
their own lead form; everyone else falls back to the trade-in widget's DEMO
flow, with the vehicle's year/make/model pre-populated in the URL either way.

Matching convention: the platform's business `dealer_id` equals the fyiAuto
`tbl_dealer.id` (as a string). Enroll a dealer by adding that id to the platform
`dealers` table with a LEAD_FORM grant in `dealer_products`.

Config (env, see .env.example):
    PLATFORM_DB_PATH        path to the shared platform.db (enrollment source)
    LEAD_FORM_BASE_URL      base of the Dealer Lead Form app (route /d/<id>)
    TRADEIN_FALLBACK_URL    URL used when the dealer is not enrolled
"""

import datetime
import os
import sqlite3
from urllib.parse import urlencode

PLATFORM_DB_PATH = os.environ.get(
    "PLATFORM_DB_PATH", "/Users/markrankin/claude/platform/platform.db"
)
LEAD_FORM_BASE_URL = os.environ.get("LEAD_FORM_BASE_URL", "http://10.1.1.117:5002")
TRADEIN_FALLBACK_URL = os.environ.get(
    "TRADEIN_FALLBACK_URL", "http://10.1.1.117:5001/t/DEMO/condition"
)


def is_lead_form_dealer(dealer_id):
    """True if this dealer has an active LEAD_FORM grant in the platform DB."""
    if dealer_id in (None, ""):
        return False
    try:
        con = sqlite3.connect(PLATFORM_DB_PATH)
        try:
            today = datetime.date.today().isoformat()
            row = con.execute(
                "SELECT 1 FROM dealer_products "
                "WHERE dealer_id = ? AND product_code = 'LEAD_FORM' "
                "AND (valid_from IS NULL OR valid_from <= ?) "
                "AND (valid_to IS NULL OR valid_to >= ?) LIMIT 1",
                (str(dealer_id), today, today),
            ).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception:
        # Platform DB unreachable -> treat as not enrolled (use fallback link).
        return False


def availability_url(dealer_id, year=None, make=None, model=None, image=None):
    """The 'Check Availability' destination for a vehicle, with YMM (and the
    primary image as IMG, when present) prefilled."""
    qs = urlencode([(k, v) for k, v in
                    (("year", year), ("make", make), ("model", model), ("IMG", image))
                    if v])
    if is_lead_form_dealer(dealer_id):
        base = f"{LEAD_FORM_BASE_URL}/d/{dealer_id}"
    else:
        base = TRADEIN_FALLBACK_URL
    sep = "&" if "?" in base else "?"
    return base + (sep + qs if qs else "")
