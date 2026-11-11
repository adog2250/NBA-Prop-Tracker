"""ZenStats: player history, team statistics, box scores, and PWA."""

import json
import logging
import mimetypes
import os

from datetime import date, datetime
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

    return (
        date(year, 10, 1),
        date(year + 1, 7, 1),
    )


def summarize_teams(rows):
    grouped = {}

    for (
        team_id,
        name,
        abbreviation,
        scored,
        allowed,
        own,
        opponent,
    ) in rows:
        if (
            scored is None
            or allowed is None
            or scored == allowed
        ):
            continue

        item = grouped.setdefault(team_id, {
            "id": team_id,
            "name": name,
            "team": abbreviation,
            "games": 0,
            "wins": 0,
            "losses": 0,
            "_points": [],
            "_allowed": [],
            "_rebounds": [],
            "_rebounds_allowed": [],
            "_assists": [],
            "_turnovers": [],
            "_fg": [],
            "_three": [],
            "_ft": [],
        })

        own = own or {}
        opponent = opponent or {}

        item["games"] += 1
        item["wins" if scored > allowed else "losses"] += 1

        item["_points"].append(float(scored))
        item["_allowed"].append(float(allowed))

        for target, source, field in (
            ("_rebounds", own, "rebounds"),
            ("_rebounds_allowed", opponent, "rebounds"),
            ("_assists", own, "assists"),
            ("_turnovers", own, "turnovers"),
        ):
            if source.get(field) is not None:
                item[target].append(float(source[field]))

        for target, made, attempted in (
            (
                "_fg",
                "field_goals_made",
                "field_goals_attempted",
            ),
            (
                "_three",
                "threes_made",
                "threes_attempted",
            ),
            (
                "_ft",
                "free_throws_made",
                "free_throws_attempted",
            ),
        ):
            if (
                own.get(made) is not None
                and own.get(attempted) is not None
            ):
                item[target].append((
                    float(own[made]),
                    float(own[attempted]),
                ))

    for item in grouped.values():
        item["box_games"] = len(item["_rebounds"])

        item["opponent_box_games"] = len(
            item["_rebounds_allowed"]
        )

        for target, source in (
            ("points", "_points"),
            ("points_allowed", "_allowed"),
            ("rebounds", "_rebounds"),
            ("rebounds_allowed", "_rebounds_allowed"),
            ("assists", "_assists"),
            ("turnovers", "_turnovers"),
        ):
            values = item.pop(source)

            item[target] = (
                sum(values) / len(values)
                if values
                else None
            )

        for target, source in (
            ("fg_pct", "_fg"),
            ("three_pct", "_three"),
            ("ft_pct", "_ft"),
        ):
            values = item.pop(source)

            attempts = sum(
                pair[1]
                for pair in values
            )

            item[target] = (
                100 * sum(pair[0] for pair in values) / attempts
                if attempts
                else None
            )

    return grouped


def team_stats(cur, first, last, day):
    cur.execute(
        """
        WITH sides AS (
            SELECT
                id,
                home_team_id AS team_id,
                away_team_id AS opponent_id,
                home_score AS scored,
                away_score AS allowed
            FROM games
            WHERE status = 'final'
              AND game_date >= %s
              AND game_date < %s
              AND game_date <= %s

            UNION ALL

            SELECT
                id,
                away_team_id AS team_id,
                home_team_id AS opponent_id,
                away_score AS scored,
                home_score AS allowed
            FROM games
            WHERE status = 'final'
              AND game_date >= %s
              AND game_date < %s
              AND game_date <= %s
        )
        SELECT
            x.team_id,
            t.name,
            t.abbreviation,
            x.scored,
            x.allowed,
            to_jsonb(s),
            to_jsonb(o)
        FROM sides x
        JOIN teams t ON t.id = x.team_id
        LEFT JOIN team_game_stats s
          ON s.game_id = x.id::text
         AND s.team_id = x.team_id
        LEFT JOIN team_game_stats o
          ON o.game_id = x.id::text
         AND o.team_id = x.opponent_id
        """,
        (first, last, day, first, last, day),
    )

    return summarize_teams(cur.fetchall())


LOG_SELECT = """
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
"""


def make_log(row):
    (
        pid,
        gid,
        game_date,
        team_id,
        home_id,
        home,
        away,
        mins,
        pts,
        reb,
        ast,
        threes,
        stl,
        blk,
        attempts,
        stamp,
        raw,
    ) = row

    if mins is None or float(mins) <= 0:
        return None

    raw = raw or {}

    return {
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


def dashboard(day):
    first, last = season_bounds(day)
    history_first = date(first.year - 1, 10, 1)
    updated = None

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
                    "id",
                    "home_id",
                    "home",
                    "away_id",
                    "away",
                    "status",
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
                "h2h_logs": [],
            }
            for row in cur.fetchall()
        }

        # Current-season games for averages and All/Home/Away.
        cur.execute(
            LOG_SELECT + """
            WHERE p.is_active = TRUE
              AND g.status = 'final'
              AND g.game_date >= %s
              AND g.game_date < %s
              AND g.game_date <= %s
            ORDER BY g.game_date DESC, g.id DESC
            """,
            (first, last, day),
        )

        for row in cur.fetchall():
            log = make_log(row)

            if row[0] not in players or log is None:
                continue

            players[row[0]]["logs"].append(log)
            stamp = row[15]

            if stamp and (
                updated is None or stamp > updated
            ):
                updated = stamp

        # Previous-season meetings against today's opponents.
        # Historical team membership comes from that game's box score.
        cur.execute(
            """
            WITH targets AS (
                SELECT
                    home_team_id AS team_id,
                    away_team_id AS opponent_id
                FROM games
                WHERE game_date = %s

                UNION

                SELECT
                    away_team_id AS team_id,
                    home_team_id AS opponent_id
                FROM games
                WHERE game_date = %s
            )
            """ + LOG_SELECT + """
            WHERE p.is_active = TRUE
              AND g.status = 'final'
              AND g.game_date >= %s
              AND g.game_date < %s
              AND s.team_id IN (
                  g.home_team_id,
                  g.away_team_id
              )
              AND EXISTS (
                  SELECT 1
                  FROM targets target
                  WHERE target.team_id = p.current_team_id
                    AND target.opponent_id = CASE
                        WHEN s.team_id = g.home_team_id
                        THEN g.away_team_id
                        ELSE g.home_team_id
                    END
              )
            ORDER BY g.game_date DESC, g.id DESC
            """,
            (day, day, history_first, first),
        )

        for row in cur.fetchall():
            log = make_log(row)

            if row[0] in players and log is not None:
                players[row[0]]["h2h_logs"].append(log)

        teams = team_stats(cur, first, last, day)

    return {
        "date": day.isoformat(),
        "season": f"{first.year}–{last.year % 100:02d}",
        "h2h_seasons": (
            f"{history_first.year}–{first.year % 100:02d} + "
            f"{first.year}–{last.year % 100:02d}"
        ),
        "games": games,
        "players": list(players.values()),
        "team_stats": teams,
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

            return respond(
                start_response,
                dashboard(day),
            )

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
