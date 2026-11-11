"""Save official team box scores for current-season team cards."""

import os

from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg

from backfill_season import fetch, get_teams


def count(value):
    if value is None:
        return None

    text = str(value).strip().replace(",", "")

    if text.upper() in ("", "--", "-", "N/A"):
        return None

    if not text.isdigit():
        raise ValueError("Invalid team box-score count")

    return int(text)


def shot_pair(value):
    if value is None:
        return None, None

    text = str(value).strip()

    if text.upper() in ("", "--", "-", "N/A"):
        return None, None

    pieces = text.split("-")

    if len(pieces) != 2:
        raise ValueError("Invalid made/attempted team total")

    made, attempted = (
        count(piece)
        for piece in pieces
    )

    if (
        made is None
        or attempted is None
        or made > attempted
    ):
        raise ValueError("Invalid made/attempted team total")

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

    return (
        count(stats.get("totalRebounds")),
        count(stats.get("assists")),
        count(turnovers),
        *fg,
        *threes,
        *ft,
    )


def main():
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
                """
                SELECT
                    g.id,
                    g.home_team_id,
                    g.away_team_id
                FROM games g
                WHERE g.status = 'final'
                  AND g.game_date >= %s
                  AND g.game_date <= %s
                  AND (
                    g.game_date >= %s::date - 7

                    OR NOT EXISTS (
                        SELECT 1
                        FROM team_game_stats s
                        WHERE s.game_id = g.id::text
                          AND s.team_id = g.home_team_id
                    )

                    OR NOT EXISTS (
                        SELECT 1
                        FROM team_game_stats s
                        WHERE s.game_id = g.id::text
                          AND s.team_id = g.away_team_id
                    )
                  )
                ORDER BY g.game_date DESC, g.id DESC
                """,
                (first, today, today),
            )

            games = cursor.fetchall()
            connection.commit()

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

                    competitions = (
                        (payload.get("header") or {}).get(
                            "competitions"
                        )
                        or []
                    )

                    if not competitions:
                        raise ValueError(
                            "Missing game status"
                        )

                    status = (
                        (
                            competitions[0].get("status")
                            or {}
                        ).get("type")
                        or {}
                    )

                    if status.get("completed") is not True:
                        print(
                            f"{game_id}: skipped unfinished event",
                            flush=True,
                        )
                        continue

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
                            "Missing one team's box score"
                        )

                    for team_id, values in rows.items():
                        if any(
                            value is None
                            for value in values
                        ):
                            raise ValueError(
                                "Incomplete team totals; will retry"
                            )

                        cursor.execute(
                            """
                            INSERT INTO team_game_stats (
                                game_id,
                                team_id,
                                rebounds,
                                assists,
                                turnovers,
                                field_goals_made,
                                field_goals_attempted,
                                threes_made,
                                threes_attempted,
                                free_throws_made,
                                free_throws_attempted
                            )
                            VALUES (
                                %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s
                            )
                            ON CONFLICT (game_id, team_id)
                            DO UPDATE SET
                                rebounds = EXCLUDED.rebounds,
                                assists = EXCLUDED.assists,
                                turnovers = EXCLUDED.turnovers,
                                field_goals_made =
                                    EXCLUDED.field_goals_made,
                                field_goals_attempted =
                                    EXCLUDED.field_goals_attempted,
                                threes_made =
                                    EXCLUDED.threes_made,
                                threes_attempted =
                                    EXCLUDED.threes_attempted,
                                free_throws_made =
                                    EXCLUDED.free_throws_made,
                                free_throws_attempted =
                                    EXCLUDED.free_throws_attempted,
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
