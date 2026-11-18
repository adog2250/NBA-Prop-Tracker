"""Load NFL schedules, skipping games with undecided teams."""

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


def season_months(season):
    for month in range(8, 13):
        yield f"{season}{month:02d}"

    for month in range(1, 8):
        yield f"{season + 1}{month:02d}"


def real_team_id(competitor):
    value = str(
        (competitor.get("team") or {}).get("id") or ""
    )

    if not value.isdigit() or int(value) <= 0:
        return None

    return value


def collect_games(season):
    games = {}
    undecided = set()

    for month in season_months(season):
        payload = fetch(
            f"{BASE}/scoreboard?dates={month}&limit=1000"
        )

        events = payload.get("events")

        if not isinstance(events, list):
            raise ValueError(
                f"Schedule response for {month} has no event list"
            )

        imported = 0

        for event in events:
            event_season = event.get("season") or {}

            if int(event_season.get("year", 0)) != season:
                continue

            season_type = int(event_season.get("type") or 0)

            if season_type not in (1, 2, 3):
                continue

            game_id = str(event["id"])
            competitions = event.get("competitions") or []

            if len(competitions) != 1:
                raise ValueError(
                    f"Unexpected competition data: {game_id}"
                )

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

            home_id = real_team_id(home or {})
            away_id = real_team_id(away or {})

            if home_id is None or away_id is None:
                if status in {"final", "in_progress"}:
                    raise ValueError(
                        f"Started game {game_id} has invalid teams"
                    )

                undecided.add(game_id)
                continue

            if home_id == away_id:
                raise ValueError(
                    f"Identical home/away teams: {game_id}"
                )

            starts_at = datetime.fromisoformat(
                event["date"].replace("Z", "+00:00")
            )

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
                season_type,
                (event.get("week") or {}).get("number"),
                starts_at,
                home_id,
                away_id,
                status,
                home_score,
                away_score,
            )

            imported += 1

        print(
            f"{month}: {imported} games with known teams checked",
            flush=True,
        )

    if not games:
        raise ValueError(
            f"No games with known teams returned for {season}. "
            "Existing data was left unchanged."
        )

    print(
        f"Future games with undecided teams skipped: {len(undecided)}",
        flush=True,
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
                    "NFL team records are missing. "
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
