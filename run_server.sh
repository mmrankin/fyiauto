#!/bin/bash
# fyiAuto web server — run as a persistent LaunchAgent (KeepAlive) so the site
# stays live at http://<host>:5055 independent of any editor/preview session.
# Production server: waitress via serve_prod.py (not the Flask dev server).
cd /Users/markrankin/fyiAuto || exit 1
export PORT=5055
exec /Users/markrankin/fyiAuto/.venv/bin/python \
     /Users/markrankin/fyiAuto/serve_prod.py
