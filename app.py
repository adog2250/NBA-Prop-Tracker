"""ZenStats: saved game logs, player box scores, and PWA assets."""

import json
import logging
import mimetypes
import os
from datetime import datetime, date
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import psycopg

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def database():
    return psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=5,
        sslmode="require",
    )


def respond(
    start,
    data,
    status="200 OK",
    content_type="application/json; charset=utf-8",
):
    body = (
        data
        if isinstance(data, bytes)
        else json.dumps(data, default=str).encode()
    )

    start(status, [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ])

    return [body]


def season_bounds(day):
    year = day.year if day.month >= 10 else day.year - 1
    return date(year, 10, 1), date(year + 1, 7, 1)


def dashboard(day):
    first, last = season_bounds(day)

    with database() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                g.id,
                ht.id,
                ht.abbreviation,
                at.id,
                at.abbreviation,
                g.status
            FROM games g
            JOIN teams ht ON ht.id = g.home_team_id
            JOIN teams at ON at.id = g.away_team_id
            WHERE g.game_date = %s
            ORDER BY g.id
            """,
            (day,),
        )

        games = [
            dict(zip(
                (
                    "id", "home_id", "home",
                    "away_id", "away", "status",
                ),
                row,
            ))
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT
                p.id,
                p.full_name,
                p.current_team_id,
                t.abbreviation,
                to_jsonb(p)->>'listed_position',
                to_jsonb(p)->>'listed_height',
                to_jsonb(p)->>'listed_weight'
            FROM players p
            LEFT JOIN teams t ON t.id = p.current_team_id
            WHERE p.is_active = TRUE
            ORDER BY p.full_name
            """
        )

        players = {
            row[0]: {
                "id": row[0],
                "name": row[1],
                "team_id": row[2],
                "team": row[3] or "",
                "position": row[4],
                "height": row[5],
                "weight": row[6],
                "logs": [],
            }
            for row in cur.fetchall()
        }

        cur.execute(
            """
            SELECT
                s.player_id,
                g.id,
                g.game_date,
                s.team_id,
                g.home_team_id,
                ht.abbreviation,
                at.abbreviation,
                s.minutes,
                s.points,
                s.rebounds,
                s.assists,
                s.threes_made,
                s.steals,
                s.blocks,
                (to_jsonb(s)->>'threes_attempted')::integer,
                g.updated_at,
                to_jsonb(s)
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            JOIN teams ht ON ht.id = g.home_team_id
            JOIN teams at ON at.id = g.away_team_id
            JOIN players p ON p.id = s.player_id
            WHERE p.is_active = TRUE
              AND g.status = 'final'
              AND g.game_date >= %s
              AND g.game_date < %s
              AND g.game_date <= %s
            ORDER BY g.game_date DESC, g.id DESC
            """,
            (first, last, day),
        )

        updated = None

        for row in cur.fetchall():
            (
                pid, gid, game_date, team_id, home_id,
                home, away, mins, pts, reb, ast,
                threes, stl, blk, attempts, stamp, raw,
            ) = row

            if pid not in players:
                continue

            if mins is None or float(mins) <= 0:
                continue

            log = {
                "id": str(gid),
                "date": game_date.isoformat(),
                "home": team_id == home_id,
                "opp": away if team_id == home_id else home,
                "home_team": home,
                "away_team": away,
                "MIN": float(mins),
                "PTS": pts,
                "REB": reb,
                "AST": ast,
                "3PM": threes,
                "3PA": attempts,
                "STL": stl,
                "BLK": blk,
                "TOV": raw.get("turnovers"),
                "FGM": raw.get("field_goals_made"),
                "FGA": raw.get("field_goals_attempted"),
                "FTM": raw.get("free_throws_made"),
                "FTA": raw.get("free_throws_attempted"),
                "PF": raw.get("personal_fouls"),
                "OREB": raw.get("offensive_rebounds"),
                "DREB": raw.get("defensive_rebounds"),
            }

            players[pid]["logs"].append(log)

            if stamp and (updated is None or stamp > updated):
                updated = stamp

    return {
        "date": day.isoformat(),
        "season": f"{first.year}–{last.year % 100:02d}",
        "games": games,
        "players": list(players.values()),
        "updated_at": updated,
    }


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")

    if environ.get("REQUEST_METHOD", "GET") != "GET":
        return respond(
            start_response,
            {"error": "Method not allowed"},
            "405 Method Not Allowed",
        )

    files = {
        "/": "index.html",
        "/manifest.webmanifest": "manifest.webmanifest",
        "/service-worker.js": "service-worker.js",
        "/icon-192.png": "icon-192.png",
        "/icon-512.png": "icon-512.png",
        "/apple-touch-icon.png": "apple-touch-icon.png",
    }

    if path in files:
        file = STATIC / files[path]

        types = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".webmanifest": "application/manifest+json",
        }

        if file.is_file():
            content_type = types.get(
                file.suffix,
                mimetypes.guess_type(str(file))[0]
                or "application/octet-stream",
            )

            return respond(
                start_response,
                file.read_bytes(),
                content_type=content_type,
            )

    try:
        if path == "/health":
            with database() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM teams")
                total = cur.fetchone()[0]

            return respond(start_response, {
                "status": "online",
                "database": "connected",
                "teams_in_database": total,
            })

        if path == "/api/dashboard":
            value = parse_qs(
                environ.get("QUERY_STRING", "")
            ).get("date", [None])[0]

            try:
                day = (
                    date.fromisoformat(value)
                    if value
                    else datetime.now(
                        ZoneInfo("America/Chicago")
                    ).date()
                )
            except ValueError:
                return respond(
                    start_response,
                    {"error": "Invalid date"},
                    "400 Bad Request",
                )

            return respond(start_response, dashboard(day))

    except Exception:
        logging.exception("ZenStats request failed")

        return respond(
            start_response,
            {
                "error":
                    "Saved NBA data is temporarily unavailable. "
                    "Try again shortly."
            },
            "503 Service Unavailable",
        )

    return respond(
        start_response,
        {"error": "Not found"},
        "404 Not Found",
    )


application = app
