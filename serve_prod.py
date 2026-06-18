"""Production WSGI entrypoint — serves the fyiAuto Flask app with waitress
(threaded, no fork; well-behaved on macOS and Linux). Port comes from $PORT.

Run via run_server.sh / the com.fyiauto.web LaunchAgent (or systemd on Linux).
Importing `app` starts its background cache-warm thread, so the first real
request hits a warm cache.
"""

import os

from waitress import serve

from app import app

if __name__ == "__main__":
    serve(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5055")),
        threads=int(os.environ.get("WAITRESS_THREADS", "8")),
        ident="fyiAuto",
    )
