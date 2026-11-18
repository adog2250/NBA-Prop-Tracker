"""Load the current NFL season schedule without changing NBA data."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from sync_nfl_rosters import BASE, fetch


def current_season():
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    return today.year if today.month >= 8 else today.year - 1


def score_value(value):
    if value is None or value == "":
        return None
    return int(value)


def collect_games(season):
    games = {}
    date_range = f"{season}0801-{season + 1}0731"

    for season_type in (1, 2, 3):
        payload = fetch(
            f"{BASE}/scoreboard"
            f"?dates={date_range}"
            f"&seasontype={season_type}"
            f"&limit=1000"
        )

        events = payload.get("events")

        if not isinstance(events, list):
            raise ValueError("Schedule response has no event list")

        for event in events:
            event_season = event.get("season") or {}

            if int(event_season.get("year", 0)) != season:
                continue

            actual_type = int(
                event_season.get("type") or season_type
            )

            competitions = event.get("competitions") or []

            if len(competitions) != 1:
                raise ValueError("Unexpected game competition data")

            competition = competitions[0]
            competitors = competition.get("competitors") or []

            home = next(
                (
                    item for item in competitors
                    if item.get("homeAway") == "home"
                ),
                None,
            )

            away = next(
                (
                    item for item in competitors
                    if item.get("homeAway") == "away"
                ),
                None,
            )

            if home is None or away is None:
                raise ValueError("Game is missing home/away teams")

            game_id = str(event["id"])
            home_id = str(home["team"]["id"])
            away_id = str(away["team"]["id"])

            if home_id == away_id:
                raise ValueError("Game has identical home/away teams")

            starts_at = datetime.fromisoformat(
                event["date"].replace("Z", "+00:00")
            )

            status_info = (
                event.get("status")
                or competition.get("status")
                or {}
            ).get("type") or {}

            if status_info.get("completed") is True:
                status = "final"
            elif status_info.get("state") == "in":
                status = "in_progress"
            else:
                status = (
                    status_info.get("name") or "scheduled"
                ).lower().removeprefix("status_")

            home_score = None
            away_score = None

            if status in {"final", "in_progress"}:
                home_score = score_value(home.get("score"))
                away_score = score_value(away.get("score"))

            if status == "final" and (
                home_score is None or away_score is None
            ):
                raise ValueError(
                    f"Final game {game_id} lacks scores"
                )

            games[game_id] = (
                game_id,
                season,
                actual_type,
                (event.get("week") or {}).get("number"),
                starts_at,
                home_id,
                away_id,
                status,
                home_score,
                away_score,
            )

    if not games:
        raise ValueError(
            f"No games returned for season {season}; "
            "existing data was left unchanged."
        )

    return list(games.values())


def main():
    season = int(
        os.environ.get("NFL_SEASON") or current_season()
    )
    rows = collect_games(season)

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM public.nfl_teams")
            known_teams = {row[0] for row in cursor.fetchall()}

            missing_teams = {
                team_id
                for row in rows
                for team_id in (row[5], row[6])
                if team_id not in known_teams
            }

            if missing_teams:
                raise RuntimeError(
                    "Run the NFL roster collector first. "
                    f"Missing team IDs: {sorted(missing_teams)}"
                )

            cursor.executemany(
                """
                INSERT INTO public.nfl_games (
                    id,
                    season,
                    season_type,
                    week,
                    starts_at,
                    home_team_id,
                    away_team_id,
                    status,
                    home_score,
                    away_score
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    season = EXCLUDED.season,
                    season_type = EXCLUDED.season_type,
                    week = EXCLUDED.week,
                    starts_at = EXCLUDED.starts_at,
                    home_team_id = EXCLUDED.home_team_id,
                    away_team_id = EXCLUDED.away_team_id,
                    status = EXCLUDED.status,
                    home_score = EXCLUDED.home_score,
                    away_score = EXCLUDED.away_score,
                    updated_at = NOW()
                """,
                rows,
            )

    print(f"NFL season: {season}")
    print(f"Games updated: {len(rows)}")
    print("NBA data unchanged.")


if __name__ == "__main__":
    main()
