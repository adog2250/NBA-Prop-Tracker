"""Collect complete final team box scores; repair missing/incomplete rows."""

import argparse
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
from backfill_season import fetch, get_teams


FIELDS = (
    "rebounds",
    "assists",
    "turnovers",
    "field_goals_made",
    "field_goals_attempted",
    "threes_made",
    "threes_attempted",
    "free_throws_made",
    "free_throws_attempted",
)


def count(value):
    if value is None:
        return None

    text = str(value).strip().replace(",", "")

    if text.upper() in ("", "--", "-", "N/A"):
        return None

    if not text.isdigit():
        raise ValueError(f"Invalid team count: {value!r}")

    return int(text)


def shot_pair(value):
    if value is None:
        return None, None

    text = str(value).strip()

    if text.upper() in ("", "--", "-", "N/A"):
        return None, None

    pieces = text.split("-")

    if len(pieces) != 2:
        raise ValueError(
            f"Invalid made/attempted total: {value!r}"
        )

    made, attempted = (
        count(piece)
        for piece in pieces
    )

    if (
        made is None
        or attempted is None
        or made > attempted
    ):
        raise ValueError(
            f"Invalid made/attempted total: {value!r}"
        )

    return made, attempted


def team_values(group):
    stats = {
        item.get("name"): item.get("displayValue")
        for item in group.get("statistics") or []
    }

    fg = shot_pair(
        stats.get("fieldGoalsMade-fieldGoalsAttempted")
    )

    threes = shot_pair(
        stats.get(
            "threePointFieldGoalsMade-threePointFieldGoalsAttempted"
        )
    )

    ft = shot_pair(
        stats.get("freeThrowsMade-freeThrowsAttempted")
    )

    turnovers = stats.get("totalTurnovers")

    if turnovers is None:
        turnovers = stats.get("turnovers")

    values = (
        count(stats.get("totalRebounds")),
        count(stats.get("assists")),
        count(turnovers),
        *fg,
        *threes,
        *ft,
    )

    if any(value is None for value in values):
        raise ValueError(
            "Incomplete team totals; saved data retained"
        )

    if threes[0] > fg[0] or threes[1] > fg[1]:
        raise ValueError(
            "Three-point totals exceed field-goal totals"
        )

    return values


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--full-season",
        action="store_true",
        help="Recheck every saved final game this season",
    )

    args = parser.parse_args()

    today = datetime.now(
        ZoneInfo("America/Chicago")
    ).date()

    year = (
        today.year
        if today.month >= 10
        else today.year - 1
    )

    first = date(year, 10, 1)
    failed = []
    updated = 0

    # These identifiers are fixed constants,
    # never user input.
    complete = " AND ".join(
        f"s.{field} IS NOT NULL"
        for field in FIELDS
    )

    columns = ", ".join(FIELDS)

    updates = ", ".join(
        f"{field} = EXCLUDED.{field}"
        for field in FIELDS
    )

    placeholders = ", ".join(
        ["%s"] * (2 + len(FIELDS))
    )

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            teams = get_teams(cursor)

            if "WAS" in teams:
                teams["WSH"] = teams["WAS"]

            cursor.execute(
                f"""
                SELECT
                    g.id,
                    g.home_team_id,
                    g.away_team_id
                FROM games g
                WHERE g.status = 'final'
                  AND g.game_date >= %s
                  AND g.game_date <= %s
                  AND (
                    %s

                    OR g.game_date >= %s::date - 7

                    OR NOT EXISTS (
                        SELECT 1
                        FROM team_game_stats s
                        WHERE s.game_id = g.id::text
                          AND s.team_id = g.home_team_id
                          AND {complete}
                    )

                    OR NOT EXISTS (
                        SELECT 1
                        FROM team_game_stats s
                        WHERE s.game_id = g.id::text
                          AND s.team_id = g.away_team_id
                          AND {complete}
                    )
                  )
                ORDER BY g.game_date DESC, g.id DESC
                """,
                (
                    first,
                    today,
                    args.full_season,
                    today,
                ),
            )

            games = cursor.fetchall()
            connection.commit()

            print(
                f"Final games to check: {len(games)}",
                flush=True,
            )

            for game_id, home_id, away_id in games:
                try:
                    payload = fetch(
                        "/summary",
                        {"event": str(game_id)},
                    )

                    if payload is None:
                        raise ValueError(
                            "ESPN summary unavailable"
                        )

                    header = payload.get("header") or {}

                    if (
                        header.get("id")
                        and str(header["id"]) != str(game_id)
                    ):
                        raise ValueError(
                            "Summary event ID does not match"
                        )

                    competitions = (
                        header.get("competitions") or []
                    )

                    status = (
                        (
                            competitions[0].get("status")
                            or {}
                        ).get("type")
                        or {}
                    ) if competitions else {}

                    if status.get("completed") is not True:
                        raise ValueError(
                            "ESPN has not marked this game final"
                        )

                    rows = {}

                    groups = (
                        (payload.get("boxscore") or {}).get(
                            "teams"
                        )
                        or []
                    )

                    for group in groups:
                        abbreviation = str(
                            (
                                group.get("team")
                                or {}
                            ).get("abbreviation")
                            or ""
                        ).upper()

                        team_id = teams.get(abbreviation)

                        if team_id in (home_id, away_id):
                            rows[team_id] = team_values(group)

                    if set(rows) != {home_id, away_id}:
                        raise ValueError(
                            "Missing one team's complete box score"
                        )

                    # Both teams must validate before
                    # either row is written.
                    for team_id, values in rows.items():
                        cursor.execute(
                            f"""
                            INSERT INTO team_game_stats (
                                game_id,
                                team_id,
                                {columns}
                            )
                            VALUES ({placeholders})
                            ON CONFLICT (game_id, team_id)
                            DO UPDATE SET
                                {updates},
                                updated_at = NOW()
                            """,
                            (
                                str(game_id),
                                team_id,
                                *values,
                            ),
                        )

                    connection.commit()
                    updated += 1

                    print(
                        f"{game_id}: both team box scores saved",
                        flush=True,
                    )

                except Exception as error:
                    connection.rollback()
                    failed.append(str(game_id))

                    print(
                        f"{game_id}: {error}",
                        flush=True,
                    )

    print(
        f"Games updated: {updated}",
        flush=True,
    )

    if failed:
        raise RuntimeError(
            "Retry games: " + ", ".join(failed)
        )


if __name__ == "__main__":
    main()
