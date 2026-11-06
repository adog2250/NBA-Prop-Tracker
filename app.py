
import json
import os
from datetime import datetime, timezone

import psycopg


def respond(start_response, data, status="200 OK"):
    body = json.dumps(data).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")

    if path == "/":
        return respond(start_response, {
            "service": "NBA Prop Tracker",
            "status": "online",
            "database": "check /health",
        })

    if path == "/health":
        database_url = os.environ.get("DATABASE_URL")

        if not database_url:
            return respond(start_response, {
                "service": "NBA Prop Tracker",
                "database": "not_configured",
            }, "503 Service Unavailable")

        try:
            with psycopg.connect(
                database_url,
                connect_timeout=5,
                sslmode="require",
            ) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM teams")
                    team_count = cursor.fetchone()[0]

            return respond(start_response, {
                "service": "NBA Prop Tracker",
                "status": "online",
                "database": "connected",
                "teams_in_database": team_count,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })

        except Exception:
            return respond(start_response, {
                "service": "NBA Prop Tracker",
                "status": "online",
                "database": "connection_failed",
            }, "503 Service Unavailable")

    return respond(start_response, {
        "error": "Not found",
    }, "404 Not Found")
