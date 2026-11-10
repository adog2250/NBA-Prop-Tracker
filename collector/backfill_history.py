"""Import prior-season box scores, skipping unfinished games."""

import argparse
import calendar
import os

from datetime import date, timedelta

import psycopg
import backfill_season as source


def month_range(season_start, month):
    valid_months = (10, 11, 12, 1, 2, 3, 4, 5, 6)

    if (
        not 2000 <= season_start <= 2100
        or month not in valid_months
    ):
        raise ValueError(
            "Choose a valid season start year and NBA season month."
        )

    year = (
        season_start
        if month >= 10
        else season_start + 1
    )

    return (
        date(year, month, 1),
        date(
            year,
            month,
            calendar.monthrange(year, month)[1],
        ),
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--season-start",
        type=int,
        required=True,
    )

    parser.add_argument(
        "--month",
        type=int,
        required=True,
    )

    args = parser.parse_args()

    first, last = month_range(
        args.season_start,
        args.month,
    )

    source.SEASON = (
        f"{args.season_start}-"
        f"{(args.season_start + 1) % 100:02d}"
    )

    failed = []
    imported = 0
    skipped = 0

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            teams = source.get_teams(cursor)

            if "WAS" in teams:
                teams["WSH"] = teams["WAS"]

            day = first

            while day <= last:
                scoreboard = source.fetch(
                    "/scoreboard",
                    {
                        "dates": day.strftime("%Y%m%d"),
                        "limit": 100,
                        "seasontype": 2,
                    },
                )

                if scoreboard is None:
                    failed.append(str(day))
                    day += timedelta(days=1)
                    continue

                for event in scoreboard.get("events") or []:
                    season_type = str(
                        (event.get("season") or {}).get(
                            "type", 2
                        )
                    )

                    if season_type != "2":
                        continue

                    game_id = str(
                        event.get("id") or ""
                    )

                    if not game_id:
                        continue

                    status = (
                        (event.get("status") or {}).get("type")
                        or {}
                    )

                    # "post" can also mean postponed.
                    # Require an explicitly completed game.
                    if status.get("completed") is not True:
                        skipped += 1

                        name = str(
                            status.get("name") or ""
                        ).upper()

                        corrected_status = {
                            "STATUS_POSTPONED": "postponed",
                            "STATUS_CANCELED": "canceled",
                            "STATUS_CANCELLED": "canceled",
                        }.get(name)

                        if corrected_status:
                            cursor.execute(
                                """
                                UPDATE games
                                SET status = %s,
                                    stats_verified = FALSE,
                                    updated_at = NOW()
                                WHERE id = %s
                                  AND status = 'final'
                                """,
                                (
                                    corrected_status,
                                    game_id,
                                ),
                            )

                            connection.commit()

                        continue

                    try:
                        saved_id = source.save_game(
                            cursor,
                            event,
                            day,
                            teams,
                        )

                        if saved_id is None:
                            connection.commit()
                            continue

                        count = source.save_boxscore(
                            cursor,
                            saved_id,
                            teams,
                        )

                        if count is None:
                            connection.rollback()
                            failed.append(game_id)
                        else:
                            connection.commit()
                            imported += 1

                    except Exception:
                        connection.rollback()
                        failed.append(game_id)

                        print(
                            f"Could not import game {game_id}; "
                            "retry this month.",
                            flush=True,
                        )

                print(
                    f"{day}: checked",
                    flush=True,
                )

                day += timedelta(days=1)

    print(
        f"{source.SEASON}, month {args.month}: "
        f"{imported} games imported",
        f"Unfinished or postponed events skipped: {skipped}",
        sep="\n",
        flush=True,
    )

    if failed:
        raise RuntimeError(
            "Retry this month. Missing dates or games: "
            + ", ".join(failed)
        )


if __name__ == "__main__":
    main()
