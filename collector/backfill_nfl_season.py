"""Import every completed NFL game from the selected season."""

import os

import psycopg

from sync_nfl_rosters import BASE, fetch
from sync_nfl_games import current_season
from sync_nfl_stats import parse_stats, save_game


def main():
    season = int(
        os.environ.get("NFL_SEASON") or current_season()
    )

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    home_team_id,
                    away_team_id,
                    starts_at
                FROM public.nfl_games
                WHERE season = %s
                  AND season_type IN (2, 3)
                  AND status = 'final'
                ORDER BY starts_at, id
                """,
                (season,),
            )

            games = cursor.fetchall()

    if not games:
        raise RuntimeError(
            f"No completed games found for season {season}. "
            "Run the NFL schedule collector first."
        )

    print(
        f"Season {season}: {len(games)} completed games to import",
        flush=True,
    )

    failures = []

    for index, (game_id, home_id, away_id, starts_at) in enumerate(
        games, start=1
    ):
        try:
            payload = fetch(
                f"{BASE}/summary?event={game_id}"
            )

            players = parse_stats(
                payload,
                game_id,
                {str(home_id), str(away_id)},
            )

            if not players:
                raise ValueError("No usable player stats")

            save_game(game_id, players)

            print(
                f"[{index}/{len(games)}] "
                f"{starts_at.date()} · {game_id}: "
                f"{len(players)} player box scores saved",
                flush=True,
            )

        except Exception as exc:
            failures.append(str(game_id))

            print(
                f"[{index}/{len(games)}] "
                f"{game_id}: FAILED — {exc}",
                flush=True,
            )

    # Verify that both teams have saved player stats for every final.
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT g.id
                FROM public.nfl_games g
                WHERE g.season = %s
                  AND g.season_type IN (2, 3)
                  AND g.status = 'final'
                  AND (
                      NOT EXISTS (
                          SELECT 1
                          FROM public.nfl_player_game_stats s
                          WHERE s.game_id = g.id
                            AND s.team_id = g.home_team_id
                      )
                      OR NOT EXISTS (
                          SELECT 1
                          FROM public.nfl_player_game_stats s
                          WHERE s.game_id = g.id
                            AND s.team_id = g.away_team_id
                      )
                  )
                ORDER BY g.id
                """,
                (season,),
            )

            missing = [
                str(row[0])
                for row in cursor.fetchall()
            ]

    print(f"Completed games checked: {len(games)}")
    print(f"Downloads/imports failed: {len(failures)}")
    print(f"Games missing one or both teams' stats: {len(missing)}")

    if failures or missing:
        retry = sorted(set(failures + missing))

        raise RuntimeError(
            "Backfill is not complete. Retry these games: "
            + ", ".join(retry)
        )

    print(
        "SUCCESS: every saved completed game has "
        "player box scores for both teams.",
        flush=True,
    )

    print("NBA data unchanged.")


if __name__ == "__main__":
    main()
