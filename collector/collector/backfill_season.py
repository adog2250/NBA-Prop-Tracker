
"""Backfill 2026-27 NBA regular-season game logs from ESPN.

Idempotent: never deletes historical stats.
Safe to rerun alongside sync_nba.py.
"""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
START = date(2026, 10, 20)
SEASON = "2026-27"
DELAY = float(os.getenv("ESPN_DELAY_SECONDS", "0.35"))


def fetch(path, params=None):
    url = BASE + path + ("?" + urlencode(params) if params else "")

    for attempt in range(4):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 NBA-Prop-Tracker",
                    "Accept": "application/json",
                },
            )

            with urlopen(req, timeout=30) as response:
                payload = json.load(response)

            time.sleep(DELAY)
            return payload

        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            OSError,
        ) as exc:
            print(
                f"FETCH ERROR attempt={attempt + 1} "
                f"url={url} error={exc}",
                flush=True,
            )

            if attempt < 3:
                time.sleep(2 ** attempt)

    return None


def integer(value):
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def mins(value):
    try:
        value = str(value or "0")

        if ":" in value:
            m, s = value.split(":", 1)
            return round(int(m) + int(s) / 60, 3)

        return float(value)

    except (ValueError, TypeError):
        return 0.0


def team_ids(cur):
    cur.execute("SELECT id, abbreviation")

    result = {
        str(abbr).upper(): int(tid)
        for tid, abbr in cur.fetchall()
        if abbr
    }

    aliases = {
        "GS": "GSW",
        "NY": "NYK",
        "SA": "SAS",
        "NO": "NOP",
        "UTAH": "UTA",
    }

    for alias, canonical in aliases.items():
        if canonical in result:
            result[alias] = result[canonical]

    if len(set(result.values())) < 30:
        raise RuntimeError(
            "Database must contain all 30 teams before backfill"
        )

    return result


def upsert_player(cur, athlete, current_team=None):
    raw = athlete.get("id")

    name = (
        athlete.get("displayName")
        or athlete.get("fullName")
        or athlete.get("name")
    )

    if not raw or not str(raw).isdigit() or not name:
        return None

    pid = -int(raw)

    if current_team is None:
        cur.execute(
            """
            INSERT INTO players
                (id, full_name, is_active, updated_at)
            VALUES (%s, %s, TRUE, NOW())
            ON CONFLICT (id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                updated_at = NOW()
            """,
            (pid, name),
        )
    else:
        cur.execute(
            """
            INSERT INTO players
                (id, full_name, current_team_id,
                 is_active, updated_at)
            VALUES (%s, %s, %s, TRUE, NOW())
            ON CONFLICT (id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                current_team_id = EXCLUDED.current_team_id,
                is_active = TRUE,
                updated_at = NOW()
            """,
            (pid, name, current_team),
        )

    return pid


def sync_rosters(cur, teams):
    payload = fetch("/teams")

    if payload is None:
        print(
            "WARNING: roster index unavailable; "
            "proceeding with game backfill",
            flush=True,
        )
        return 0

    sports = payload.get("sports") or []

    leagues = (
        sports[0].get("leagues") or []
    ) if sports else []

    entries = (
        leagues[0].get("teams") or []
    ) if leagues else []

    count = 0

    for entry in entries:
        team = entry.get("team") or {}

        tid = teams.get(
            str(team.get("abbreviation", "")).upper()
        )

        if tid is None or not team.get("id"):
            continue

        roster = fetch(
            "/teams/" + str(team["id"]) + "/roster"
        )

        if roster is None:
            continue

        for group in roster.get("athletes") or []:
            athletes = group.get("items") or [group]

            for athlete in athletes:
                if upsert_player(
                    cur, athlete, tid
                ) is not None:
                    count += 1

    return count


def upsert_game(cur, event, day, teams):
    competitions = event.get("competitions") or []

    if not competitions:
        return None

    competitors = (
        competitions[0].get("competitors") or []
    )

    home = next(
        (
            x for x in competitors
            if x.get("homeAway") == "home"
        ),
        None,
    )

    away = next(
        (
            x for x in competitors
            if x.get("homeAway") == "away"
        ),
        None,
    )

    if not home or not away:
        return None

    home_id = teams.get(
        str(
            (home.get("team") or {}).get(
                "abbreviation", ""
            )
        ).upper()
    )

    away_id = teams.get(
        str(
            (away.get("team") or {}).get(
                "abbreviation", ""
            )
        ).upper()
    )

    if home_id is None or away_id is None:
        return None

    game_id = str(event.get("id", ""))

    if not game_id:
        return None

    state = (
        (event.get("status") or {}).get("type")
        or {}
    )

    final = (
        bool(state.get("completed"))
        or state.get("state") == "post"
    )

    cur.execute(
        """
        INSERT INTO games
            (id, game_date, season, home_team_id,
             away_team_id, home_score, away_score,
             status, stats_verified, updated_at)
        VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s,FALSE,NOW())
        ON CONFLICT (id) DO UPDATE SET
            game_date = EXCLUDED.game_date,
            season = EXCLUDED.season,
            home_team_id = EXCLUDED.home_team_id,
            away_team_id = EXCLUDED.away_team_id,
            home_score = EXCLUDED.home_score,
            away_score = EXCLUDED.away_score,
            status = EXCLUDED.status,
            updated_at = NOW()
        """,
        (
            game_id,
            day,
            SEASON,
            home_id,
            away_id,
            integer(home.get("score")) if final else None,
            integer(away.get("score")) if final else None,
            "final" if final else "scheduled",
        ),
    )

    return game_id if final else None


def save_boxscore(cur, game_id, teams):
    payload = fetch(
        "/summary", {"event": game_id}
    )

    if payload is None:
        return None

    groups = (
        (payload.get("boxscore") or {}).get("players")
        or []
    )

    if len(groups) < 2:
        return None

    count = 0

    for group in groups:
        tid = teams.get(
            str(
                (group.get("team") or {}).get(
                    "abbreviation", ""
                )
            ).upper()
        )

        if tid is None:
            continue

        for category in group.get("statistics") or []:
            labels = [
                str(x).upper()
                for x in (
                    category.get("labels")
                    or category.get("names")
                    or []
                )
            ]

            for item in category.get("athletes") or []:
                if item.get("didNotPlay") is True:
                    continue

                stats = dict(
                    zip(labels, item.get("stats") or [])
                )

                if not stats:
                    continue

                played = (
                    mins(stats.get("MIN")) > 0
                    or any(
                        integer(stats.get(key)) > 0
                        for key in (
                            "PTS", "REB", "AST",
                            "STL", "BLK", "TO"
                        )
                    )
                )

                if not played:
                    continue

                athlete = item.get("athlete") or {}
                pid = upsert_player(cur, athlete)

                if pid is None:
                    continue

                three = stats.get(
                    "3PT",
                    stats.get(
                        "3PM-A",
                        stats.get("3PM", "0"),
                    ),
                )

                threes_made = integer(
                    str(three).split("-")[0]
                )

                cur.execute(
                    """
                    INSERT INTO player_game_stats
                        (game_id, player_id, team_id,
                         minutes, points, rebounds,
                         assists, threes_made, steals,
                         blocks, turnovers)
                    VALUES
                        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (game_id, player_id)
                    DO UPDATE SET
                        team_id = EXCLUDED.team_id,
                        minutes = EXCLUDED.minutes,
                        points = EXCLUDED.points,
                        rebounds = EXCLUDED.rebounds,
                        assists = EXCLUDED.assists,
                        threes_made = EXCLUDED.threes_made,
                        steals = EXCLUDED.steals,
                        blocks = EXCLUDED.blocks,
                        turnovers = EXCLUDED.turnovers
                    """,
                    (
                        game_id,
                        pid,
                        tid,
                        mins(stats.get("MIN")),
                        integer(stats.get("PTS")),
                        integer(stats.get("REB")),
                        integer(stats.get("AST")),
                        threes_made,
                        integer(stats.get("STL")),
                        integer(stats.get("BLK")),
                        integer(stats.get("TO")),
                    ),
                )

                count += 1

    if count >= 10:
        cur.execute(
            """
            UPDATE games
            SET stats_verified = TRUE,
                updated_at = NOW()
            WHERE id = %s
            """,
            (game_id,),
        )

    return count


def main():
    url = os.environ.get("DATABASE_URL")

    if not url:
        raise RuntimeError(
            "DATABASE_URL is missing"
        )

    end = datetime.now(timezone.utc).date()

    print(
        f"BACKFILL VERSION 2026-11-06-v2 "
        f"| {START} to {end}",
        flush=True,
    )

    failed = []

    with psycopg.connect(
        url,
        connect_timeout=15,
        sslmode="require",
    ) as conn:

        with conn.cursor() as cur:
            teams = team_ids(cur)

            roster_count = sync_rosters(cur, teams)
            conn.commit()

            print(
                f"ROSTER PLAYERS UPDATED: {roster_count}",
                flush=True,
            )

            day = START

            while day <= end:
                board = fetch(
                    "/scoreboard",
                    {
                        "dates": day.strftime("%Y%m%d"),
                        "limit": 100,
                        "seasontype": 2,
                    },
                )

                if board is None:
                    failed.append(f"scoreboard {day}")
                    day += timedelta(days=1)
                    continue

                events = board.get("events") or []
                saved = 0

                for event in events:
                    try:
                        gid = upsert_game(
                            cur, event, day, teams
                        )

                        if not gid:
                            conn.commit()
                            continue

                        # Always re-fetch final boxscores.
                        # Partial games must not be treated
                        # as fully populated games.
                        count = save_boxscore(
                            cur, gid, teams
                        )

                        if count is None or count < 10:
                            failed.append(
                                f"boxscore {gid} on {day}"
                            )
                        else:
                            saved += count

                        conn.commit()

                    except Exception as exc:
                        conn.rollback()

                        failed.append(
                            f"game {event.get('id')} "
                            f"on {day}: {exc}"
                        )

                print(
                    f"{day}: {len(events)} events; "
                    f"{saved} player-game rows",
                    flush=True,
                )

                day += timedelta(days=1)

            cur.execute(
                "SELECT COUNT(*) FROM players"
            )
            player_count = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM player_game_stats"
            )
            row_count = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM games g
                WHERE g.status = 'final'
                  AND g.game_date BETWEEN %s AND %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM player_game_stats s
                      WHERE s.game_id = g.id
                  )
                """,
                (START, end),
            )

            empty_games = cur.fetchone()[0]

            print(
                f"DATABASE CHECK: {player_count} players; "
                f"{row_count} player-game rows; "
                f"{empty_games} empty final games",
                flush=True,
            )

    if failed:
        print(
            "INCOMPLETE REQUESTS:",
            *failed,
            sep="\n- ",
            flush=True,
        )

        raise RuntimeError(
            f"{len(failed)} requests incomplete; "
            "rerun to retry"
        )

    print(
        "SEASON BACKFILL COMPLETE",
        flush=True,
    )


if __name__ == "__main__":
    main()
