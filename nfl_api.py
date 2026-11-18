"""NFL dashboard data for ZenStats."""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg


STAT_FIELDS = {
    "PASS_YDS": "passing_yards",
    "PASS_TD": "passing_touchdowns",
    "PASS_ATT": "passing_attempts",
    "PASS_CMP": "passing_completions",
    "INT": "interceptions_thrown",
    "RUSH_YDS": "rushing_yards",
    "RUSH_ATT": "rushing_attempts",
    "RUSH_TD": "rushing_touchdowns",
    "REC_YDS": "receiving_yards",
    "REC": "receptions",
    "TGTS": "targets",
    "REC_TD": "receiving_touchdowns",
    "FGM": "field_goals_made",
    "FGA": "field_goals_attempted",
    "XPM": "extra_points_made",
    "XPA": "extra_points_attempted",
    "TACKLES": "tackles_total",
    "SACKS": "sacks",
    "DEF_INT": "defensive_interceptions",
    "PD": "passes_defended",
    "FUM": "fumbles",
    "FUM_LOST": "fumbles_lost",
}

BLOCKED = {
    "out",
    "doubtful",
    "injured reserve",
    "suspended",
    "pup",
    "nfi",
    "practice squad",
}


def make_log(row):
    (
        pid,
        gid,
        starts,
        tid,
        hid,
        home,
        away,
        season,
        raw,
    ) = row

    is_home = str(tid) == str(hid)

    log = {
        "id": str(gid),
        "date": starts.astimezone(
            ZoneInfo("America/Chicago")
        ).date().isoformat(),
        "home": is_home,
        "opp": away if is_home else home,
        "home_team": home,
        "away_team": away,
        "season": season,
    }

    for key, field in STAT_FIELDS.items():
        value = (raw or {}).get(field)

        log[key] = (
            float(value)
            if value is not None
            else None
        )

    return log


def summarize_teams(rows):
    teams = {}

    for tid, name, abbreviation, scored, allowed in rows:
        if scored is None or allowed is None:
            continue

        tid = str(tid)

        item = teams.setdefault(tid, {
            "id": tid,
            "name": name,
            "team": abbreviation,
            "games": 0,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "points": 0.0,
            "points_allowed": 0.0,
        })

        item["games"] += 1
        item["points"] += float(scored)
        item["points_allowed"] += float(allowed)

        if scored > allowed:
            item["wins"] += 1
        elif scored < allowed:
            item["losses"] += 1
        else:
            item["ties"] += 1

    for item in teams.values():
        item["points"] /= item["games"]
        item["points_allowed"] /= item["games"]

    return teams


def nfl_dashboard(day):
    season = day.year if day.month >= 8 else day.year - 1
    zone = ZoneInfo("America/Chicago")

    start = datetime.combine(
        day,
        time.min,
        zone,
    )

    end = datetime.combine(
        day + timedelta(days=1),
        time.min,
        zone,
    )

    updated = None
    availability_updated = None

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=5,
        sslmode="require",
    ) as conn, conn.cursor() as cur:

        cur.execute(
            """
            SELECT week
            FROM public.nfl_games
            WHERE season = %s
              AND season_type = 2
              AND week IS NOT NULL
              AND starts_at >= %s
              AND status NOT IN ('canceled', 'cancelled')
            ORDER BY starts_at, id
            LIMIT 1
            """,
            (season, start),
        )

        row = cur.fetchone()

        if row is None:
            cur.execute(
                """
                SELECT week
                FROM public.nfl_games
                WHERE season = %s
                  AND season_type = 2
                  AND week IS NOT NULL
                ORDER BY starts_at DESC, id DESC
                LIMIT 1
                """,
                (season,),
            )

            row = cur.fetchone()

        week = row[0] if row else None

        cur.execute(
            """
            SELECT
                g.id,
                g.home_team_id,
                ht.abbreviation,
                g.away_team_id,
                at.abbreviation,
                g.status,
                g.starts_at,
                g.home_score,
                g.away_score
            FROM public.nfl_games g
            JOIN public.nfl_teams ht
              ON ht.id = g.home_team_id
            JOIN public.nfl_teams at
              ON at.id = g.away_team_id
            WHERE g.season = %s
              AND g.season_type = 2
              AND g.week = %s
              AND g.status NOT IN ('canceled', 'cancelled')
            ORDER BY g.starts_at, g.id
            """,
            (season, week),
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
                    "starts_at",
                    "home_score",
                    "away_score",
                ),
                row,
            ))
            for row in cur.fetchall()
        ]

        # Display only quarterbacks, running backs,
        # wide receivers, tight ends, and placekickers.
        cur.execute(
            """
            SELECT
                p.id,
                p.name,
                p.current_team_id,
                t.abbreviation,
                p.position,
                p.height,
                p.weight,
                p.headshot_url
            FROM public.nfl_players p
            JOIN public.nfl_teams t
              ON t.id = p.current_team_id
            WHERE p.is_active = TRUE
              AND UPPER(TRIM(COALESCE(p.position, '')))
                  IN ('QB', 'RB', 'WR', 'TE', 'K', 'PK')
            ORDER BY p.name
            """
        )

        players = {
            str(row[0]): {
                "id": str(row[0]),
                "name": row[1],
                "team_id": str(row[2]),
                "team": row[3],
                "position": row[4],
                "height": row[5],
                "weight": row[6],
                "headshot_url": row[7],
                "logs": [],
                "h2h_logs": [],
                "availability": {},
            }
            for row in cur.fetchall()
        }

        cur.execute(
            """
            SELECT
                s.player_id,
                g.id,
                g.starts_at,
                s.team_id,
                g.home_team_id,
                ht.abbreviation,
                at.abbreviation,
                g.season,
                to_jsonb(s),
                s.updated_at
            FROM public.nfl_player_game_stats s
            JOIN public.nfl_games g
              ON g.id = s.game_id
            JOIN public.nfl_players p
              ON p.id = s.player_id
            JOIN public.nfl_teams ht
              ON ht.id = g.home_team_id
            JOIN public.nfl_teams at
              ON at.id = g.away_team_id
            WHERE p.is_active = TRUE
              AND UPPER(TRIM(COALESCE(p.position, '')))
                  IN ('QB', 'RB', 'WR', 'TE', 'K', 'PK')
              AND g.season IN (%s, %s)
              AND g.season_type = 2
              AND g.status = 'final'
              AND g.starts_at < %s
              AND s.team_id IN (
                  g.home_team_id,
                  g.away_team_id
              )
            ORDER BY g.starts_at DESC, g.id DESC
            """,
            (season, season - 1, end),
        )

        for row in cur.fetchall():
            player = players.get(str(row[0]))

            if player is None:
                continue

            log = make_log(row[:9])

            target = (
                "logs"
                if row[7] == season
                else "h2h_logs"
            )

            player[target].append(log)

            if row[9] and (
                updated is None
                or row[9] > updated
            ):
                updated = row[9]

        cur.execute(
            """
            WITH sides AS (
                SELECT
                    home_team_id AS team_id,
                    home_score AS scored,
                    away_score AS allowed
                FROM public.nfl_games
                WHERE season = %s
                  AND season_type = 2
                  AND status = 'final'
                  AND starts_at < %s

                UNION ALL

                SELECT
                    away_team_id,
                    away_score,
                    home_score
                FROM public.nfl_games
                WHERE season = %s
                  AND season_type = 2
                  AND status = 'final'
                  AND starts_at < %s
            )
            SELECT
                x.team_id,
                t.name,
                t.abbreviation,
                x.scored,
                x.allowed
            FROM sides x
            JOIN public.nfl_teams t
              ON t.id = x.team_id
            """,
            (season, end, season, end),
        )

        teams = summarize_teams(cur.fetchall())

        if games:
            cur.execute(
                """
                SELECT
                    game_id,
                    player_id,
                    team_id,
                    injury_status,
                    injury_description,
                    roster_status,
                    is_starter,
                    starter_source,
                    checked_at
                FROM public.nfl_player_availability
                WHERE game_id = ANY(%s::text[])
                """,
                ([str(game["id"]) for game in games],),
            )

            for (
                gid,
                pid,
                tid,
                injury,
                description,
                roster,
                starter,
                source,
                stamp,
            ) in cur.fetchall():

                player = players.get(str(pid))

                if (
                    player is None
                    or player["team_id"] != str(tid)
                ):
                    continue

                blocked = any(
                    str(value or "").strip().lower()
                    in BLOCKED
                    for value in (injury, roster)
                )

                player["availability"][str(gid)] = {
                    "injury_status": injury,
                    "injury_description": description,
                    "roster_status": roster,
                    "is_available": not blocked,
                    "is_starter": (
                        starter is True and not blocked
                    ),
                    "starter_source": source,
                    "checked_at": stamp,
                }

                if stamp and (
                    availability_updated is None
                    or stamp > availability_updated
                ):
                    availability_updated = stamp

    return {
        "sport": "nfl",
        "date": day.isoformat(),
        "season": str(season),
        "week": week,
        "h2h_seasons": f"{season - 1} + {season}",
        "games": games,
        "players": list(players.values()),
        "team_stats": teams,
        "updated_at": updated,
        "availability_updated_at": availability_updated,
        "stat_keys": list(STAT_FIELDS),
    }
