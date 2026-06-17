#!/bin/bash
# fyiAuto web server — run as a persistent LaunchAgent (KeepAlive) so the site
# stays live at http://<host>:5055 independent of any editor/preview session.
cd /Users/markrankin/fyiAuto || exit 1
exec /Users/markrankin/fyiAuto/.venv/bin/python /Users/markrankin/fyiAuto/app.py
