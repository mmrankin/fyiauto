"""Read-only connectors to the upstream SQL Servers that feed fyiAuto.

Two sources, both reached with pymssql:

    inventory  @ 10.1.2.17   tbl_inventory / tbl_parsedImage / tbl_parsedVehicle
                              tbl_dealer / tbl_NameAddress
    vin_decode @ 10.1.1.10   VIN_Data1  (squish-VIN / vehicle_id -> YMM/trim)

Everything here is READ ONLY and production-safe: every query is issued
WITH (NOLOCK) and capped with OPTION (MAXDOP 1) so a public-site sync can
never contend with the live polling pipeline. The website itself never talks
to these servers -- only sync.py does, writing into the local SQLite store.

Configuration (environment variables, see .env.example):
    INV_DB_SERVER / INV_DB_USER / INV_DB_PASSWORD / INV_DB_DATABASE
    DECODE_DB_SERVER / DECODE_DB_USER / DECODE_DB_PASSWORD / DECODE_DB_DATABASE
"""

import os

try:
    import pymssql
except ImportError:  # pragma: no cover - sync simply can't run without it
    pymssql = None


def _cfg(prefix, default_server, default_db):
    return {
        "server": os.environ.get(f"{prefix}_SERVER", default_server),
        "user": os.environ.get(f"{prefix}_USER", "sa"),
        "password": os.environ.get(f"{prefix}_PASSWORD", ""),
        "database": os.environ.get(f"{prefix}_DATABASE", default_db),
    }


def connect_inventory():
    """Connection to the inventory warehouse on 10.1.2.17."""
    return _connect(_cfg("INV_DB", "10.1.2.17", "inventory"))


def connect_decode():
    """Connection to the VIN-decode database on 10.1.1.10."""
    return _connect(_cfg("DECODE_DB", "10.1.1.10", "vin_decode"))


def connect_ip2location():
    """Connection to the ip2Location database on 10.1.1.10 (ZIP -> lat/long)."""
    return _connect(_cfg("IP2_DB", "10.1.1.10", "ip2Location"))


def _connect(cfg):
    if pymssql is None:
        raise RuntimeError("pymssql is not installed; run pip install pymssql")
    return pymssql.connect(
        server=cfg["server"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        timeout=120,
        login_timeout=15,
        as_dict=True,
    )
