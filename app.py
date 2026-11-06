
"""NBA Prop Tracker backend health endpoint.

The NBA collector, permanent database, and roster sync are not
running in this web process yet.
"""

import json
from datetime import datetime, timezone


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")

    if path in ("/", "/health"):
        response = {
            "service": "NBA Prop Tracker",
            "status": "online",
            "collector": "not_configured",
            "database": "not_configured",
            "roster_sync": "not_configured",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        status = "200 OK"
    else:
        response = {"error": "not found"}
        status = "404 Not Found"

    body = json.dumps(response).encode("utf-8")

    start_response(
        status,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ],
    )

    return [body]
