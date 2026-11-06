
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg
from nba_api.stats.static import teams as nba_teams


ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/"
    "sports/basketball/nba/scoreboard"
)


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NBAPropTracker/1.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def sync_teams(conn):
    records = nba_teams.get_teams()

    if len(records) < 30:
        raise RuntimeError("NBA team list is incomplete")

    with conn.cursor() as cur:
        for team in records:
            cur.execute(
                """
                INSERT INTO teams
                    (id, abbreviation, name, city, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    abbreviation = EXCLUDED.abbreviation,
                    name = EXCLUDED.name,
                    city = EXCLUDED.city,
                    updated_at = NOW()
                """,
                (
                    team["id"],
                    team["abbreviation"],
                    team["full_name"],
                    team["city"],
                ),
            )

    return len(records)


def get_team_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, abbreviation FROM teams")
        return {
            abbreviation: team_id
            for team_id, abbreviation in cur.fetchall()
        }


def sync_games(conn, day):
    date_string = day.strftime("%Y%m%d")
    url = f"{ESPN_SCOREBOARD}?dates={date_string}&limit=100"
    data = fetch_json(url)

    team_ids = get_team_ids(conn)
    count = 0

    with conn.cursor() as cur:
        for event in data.get("events", []):
            competitions = event.get("competitions", [])
            if not competitions:
                continue

            competition = competitions[0]
            home = None
            away = None

            for entry in competition.get("competitors", []):
                abbreviation = entry.get("team", {}).get("abbreviation")
                team_id = team_ids.get(abbreviation)

                if entry.get("homeAway") == "home":
                    home = (team_id, entry)
                elif entry.get("homeAway") == "away":
                    away = (team_id, entry)

            if not home or not away:
                continue

            if home[0] is None or away[0] is None:
                raise RuntimeError(
                    f"Unknown team in game {event.get('id')}"
                )

            status_info = event.get("status", {}).get("type", {})
            state = status_info.get("state", "pre")
            completed = status_info.get("completed", False)

            if completed:
                status = "final"
            elif state == "in":
                status = "live"
            else:
                status = "scheduled"

            def score(entry):
                value = entry.get("score")
                if value is None or value == "":
                    return None
                return int(value)

            game_date = event.get("date", "")[:10]

            if not game_date:
                raise RuntimeError("Game is missing a date")

            season = event.get("season", {})
            season_year = season.get("year", day.year)
            season_type = season.get("type", 2)

            cur.execute(
                """
                INSERT INTO games (
                    id, game_date, season,
                    home_team_id, away_team_id,
                    home_score, away_score,
                    status, stats_verified, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, FALSE, NOW()
                )
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
                    str(event["id"]),
                    game_date,
                    f"{season_year}-{season_type}",
                    home[0],
                    away[0],
                    score(home[1]),
                    score(away[1]),
                    status,
                ),
            )
            count += 1

    return count


def main():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is missing")

    started_at = datetime.now(timezone.utc)

    with psycopg.connect(
        database_url,
        connect_timeout=10,
        sslmode="require",
    ) as conn:
        try:
            team_count = sync_teams(conn)
            conn.commit()

            today = datetime.now(timezone.utc).date()
            game_count = 0

            # Recent and upcoming games.
            for offset in range(-7, 4):
                day = today + timedelta(days=offset)
                game_count += sync_games(conn, day)
                conn.commit()

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_history (
                        job_name, started_at, completed_at,
                        status, records_processed
                    )
                    VALUES (%s, %s, NOW(), %s, %s)
                    """,
                    (
                        "nba_schedule_sync",
                        started_at,
                        "success",
                        team_count + game_count,
                    ),
                )

            conn.commit()

            print(
                f"SUCCESS: {team_count} teams, "
                f"{game_count} game records processed"
            )

        except Exception as exc:
            conn.rollback()

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_history (
                        job_name, started_at, completed_at,
                        status, error_message
                    )
                    VALUES (%s, %s, NOW(), %s, %s)
                    """,
                    (
                        "nba_schedule_sync",
                        started_at,
                        "failed",
                        str(exc)[:2000],
                    ),
                )

            conn.commit()
            raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
