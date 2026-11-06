
"""Backfill the 2026-27 NBA regular season from ESPN.

Safe to rerun: updates existing rows without deleting them.
"""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
START = date(2026, 10, 20)
SEASON = "2026-27"
DELAY = float(os.getenv("ESPN_DELAY_SECONDS", "0.4"))


def fetch(endpoint, params=None):
    url = BASE + endpoint
    if params:
        url += "?" + urlencode(params)

    for attempt in range(4):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "NBA-Prop-Tracker/1.0",
                    "Accept": "application/json",
                },
            )
            with urlopen(request, timeout=25) as response:
                result = json.load(response)

            time.sleep(DELAY)
            return result

        except Exception as exc:
            if attempt == 3:
                print(f"FETCH FAILED {url}: {exc}", flush=True)
                return None
            time.sleep(2 ** attempt)

    return None


def number(value):
    try:
        if value is None or str(value).strip() in (
            "", "--", "DNP", "N/A"
        ):
            return 0
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def minutes(value):
    try:
        if value is None or str(value).strip() in (
            "", "--", "DNP"
        ):
            return 0.0

        value = str(value)
        if ":" in value:
            a, b = value.split(":", 1)
            return int(a) + int(b) / 60

        return float(value)

    except (TypeError, ValueError):
        return 0.0


def team_map(cur):
    cur.execute("SELECT id, abbreviation FROM teams")

    result = {
        abbr.upper(): int(team_id)
        for team_id, abbr in cur.fetchall()
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

    return result


def save_player(cur, athlete, team_id):
    raw_id = athlete.get("id")

    if not raw_id or not str(raw_id).isdigit():
        return None

    pid = -int(raw_id)
    name = (
        athlete.get("displayName")
        or athlete.get("fullName")
        or athlete.get("name")
    )

    if not name:
        return None

    cur.execute(
        """
        INSERT INTO players
          (id, full_name, current_team_id, is_active, updated_at)
        VALUES (%s, %s, %s, TRUE, NOW())
        ON CONFLICT (id) DO UPDATE SET
          full_name = EXCLUDED.full_name,
          current_team_id = EXCLUDED.current_team_id,
          is_active = TRUE,
          updated_at = NOW()
        """,
        (pid, name, team_id),
    )

    return pid


def update_rosters(cur, teams):
    data = fetch("/teams") or {}

    entries = (
        data.get("sports", [{}])[0]
        .get("leagues", [{}])[0]
        .get("teams", [])
    )

    added = 0

    for entry in entries:
        team = entry.get("team", {})
        abbr = (team.get("abbreviation") or "").upper()
        team_id = teams.get(abbr)
        espn_id = team.get("id")

        if not team_id or not espn_id:
            continue

        roster = fetch(f"/teams/{espn_id}/roster") or {}

        for item in roster.get("athletes", []):
            athletes = (
                item.get("items", [])
                if "items" in item
                else [item]
            )

            for athlete in athletes:
                if save_player(cur, athlete, team_id) is not None:
                    added += 1

    return added


def save_game(cur, event, teams):
    competitions = event.get("competitions") or []

    if not competitions:
        return None

    competition = competitions[0]
    competitors = competition.get("competitors") or []

    home = next(
        (x for x in competitors if x.get("homeAway") == "home"),
        None,
    )
    away = next(
        (x for x in competitors if x.get("homeAway") == "away"),
        None,
    )

    if not home or not away:
        return None

    h = teams.get(
        (home.get("team", {}).get("abbreviation") or "").upper()
    )
    a = teams.get(
        (away.get("team", {}).get("abbreviation") or "").upper()
    )

    if not h or not a:
        return None

    game_id = str(event["id"])
    day = datetime.fromisoformat(
        event["date"].replace("Z", "+00:00")
    ).date()

    state = event.get("status", {}).get("type", {})
    finished = (
        bool(state.get("completed"))
        or state.get("state") == "post"
    )
    status = "final" if finished else "scheduled"

    cur.execute(
        """
        INSERT INTO games
          (id, game_date, season, home_team_id, away_team_id,
           home_score, away_score, status,
           stats_verified, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE,NOW())
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
            game_id, day, SEASON, h, a,
            number(home.get("score")) if finished else None,
            number(away.get("score")) if finished else None,
            status,
        ),
    )

    return game_id if finished else None


def save_boxscore(cur, game_id, teams):
    summary = fetch("/summary", {"event": game_id})

    if not summary:
        return 0

    groups = (
        (summary.get("boxscore") or {}).get("players") or []
    )

    count = 0

    for group in groups:
        abbr = (
            group.get("team", {})
            .get("abbreviation") or ""
        ).upper()

        team_id = teams.get(abbr)

        if not team_id:
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

            for entry in category.get("athletes") or []:
                if entry.get("didNotPlay") is True:
                    continue

                athlete = entry.get("athlete") or {}
                pid = save_player(cur, athlete, team_id)

                if pid is None:
                    continue

                raw = entry.get("stats") or []
                stats = dict(zip(labels, raw))

                if not stats:
                    continue

                played = (
                    minutes(stats.get("MIN")) > 0
                    or any(
                        number(stats.get(k)) > 0
                        for k in (
                            "PTS", "REB", "AST",
                            "STL", "BLK", "TO"
                        )
                    )
                )

                if not played:
                    continue

                values = (
                    number(stats.get("PTS")),
                    number(stats.get("REB")),
                    number(stats.get("AST")),
                    number(
                        str(stats.get("3PT", "0")).split("-")[0]
                    ),
                    number(stats.get("STL")),
                    number(stats.get("BLK")),
                    number(stats.get("TO")),
                )

                cur.execute(
                    """
                    INSERT INTO player_game_stats
                      (game_id, player_id, team_id, minutes,
                       points, rebounds, assists, threes_made,
                       steals, blocks, turnovers)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                        game_id, pid, team_id,
                        minutes(stats.get("MIN")),
                        *values,
                    ),
                )

                count += 1

    if count:
        cur.execute(
            """
            UPDATE games
            SET stats_verified = TRUE, updated_at = NOW()
            WHERE id = %s
            """,
            (game_id,),
        )

    return count


def main():
    url = os.environ.get("DATABASE_URL")

    if not url:
        raise RuntimeError("DATABASE_URL secret is missing")

    today = datetime.now(timezone.utc).date()

    print(
        f"SEASON BACKFILL: {START} through {today}",
        flush=True,
    )

    with psycopg.connect(
        url,
        connect_timeout=15,
        sslmode="require",
    ) as conn:

        with conn.cursor() as cur:
            teams = team_map(cur)

            if len(teams) < 30:
                raise RuntimeError(
                    f"Expected 30 NBA teams; found {len(teams)}"
                )

            roster_count = update_rosters(cur, teams)
            conn.commit()

            print(
                f"ROSTER PLAYERS UPDATED: {roster_count}",
                flush=True,
            )

            processed = 0
            rows = 0
            missing = 0
            day = START

            while day <= today:
                board = fetch(
                    "/scoreboard",
                    {
                        "dates": day.strftime("%Y%m%d"),
                        "limit": 100,
                        "seasontype": 2,
                    },
                )

                if board is None:
                    missing += 1
                    day += timedelta(days=1)
                    continue

                for event in board.get("events") or []:
                    game_id = save_game(cur, event, teams)

                    if not game_id:
                        continue

                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM player_game_stats
                        WHERE game_id = %s
                        """,
                        (game_id,),
                    )

                    existing = cur.fetchone()[0]

                    if (
                        existing >= 12
                        and day < today - timedelta(days=2)
                    ):
                        continue

                    got = save_boxscore(cur, game_id, teams)

                    rows += got
                    processed += 1

                    if got == 0:
                        missing += 1

                    conn.commit()

                conn.commit()

                print(
                    f"CHECKED {day} | "
                    f"fetched boxscores: {processed} | "
                    f"rows: {rows}",
                    flush=True,
                )

                day += timedelta(days=1)

            cur.execute("SELECT COUNT(*) FROM players")
            players = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM player_game_stats"
            )
            stat_rows = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM games g
                WHERE g.status = 'final'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM player_game_stats s
                    WHERE s.game_id = g.id
                  )
                """
            )

            unfilled = cur.fetchone()[0]

            print(
                f"DATABASE CHECK: {players} players, "
                f"{stat_rows} player-game rows, "
                f"{unfilled} final games missing stats",
                flush=True,
            )

            print(
                f"BACKFILL FINISHED: "
                f"{processed} boxscores fetched; "
                f"{missing} failures/empty results",
                flush=True,
            )

            if missing:
                raise RuntimeError(
                    f"{missing} fetches need retry on next run"
                )


if __name__ == "__main__":
    main()
