"""Import completed NFL player box scores without changing NBA data."""

import os
from decimal import Decimal, InvalidOperation

import psycopg

from sync_nfl_rosters import BASE, fetch
from sync_nfl_games import current_season


FIELDS = (
    "passing_completions",
    "passing_attempts",
    "passing_yards",
    "passing_touchdowns",
    "interceptions_thrown",
    "rushing_attempts",
    "rushing_yards",
    "rushing_touchdowns",
    "targets",
    "receptions",
    "receiving_yards",
    "receiving_touchdowns",
    "fumbles",
    "fumbles_lost",
    "field_goals_made",
    "field_goals_attempted",
    "extra_points_made",
    "extra_points_attempted",
    "tackles_total",
    "sacks",
    "defensive_interceptions",
    "passes_defended",
)

MAPS = {
    "passing": {
        "passingYards": "passing_yards",
        "passingTouchdowns": "passing_touchdowns",
        "interceptions": "interceptions_thrown",
    },
    "rushing": {
        "rushingAttempts": "rushing_attempts",
        "rushingYards": "rushing_yards",
        "rushingTouchdowns": "rushing_touchdowns",
    },
    "receiving": {
        "receivingTargets": "targets",
        "receptions": "receptions",
        "receivingYards": "receiving_yards",
        "receivingTouchdowns": "receiving_touchdowns",
    },
    "fumbles": {
        "fumbles": "fumbles",
        "fumblesLost": "fumbles_lost",
    },
    "defensive": {
        "totalTackles": "tackles_total",
        "sacks": "sacks",
        "passesDefended": "passes_defended",
    },
    "interceptions": {
        "interceptions": "defensive_interceptions",
    },
}

PAIRS = {
    "passing": {
        "completions/passingAttempts": (
            "passing_completions",
            "passing_attempts",
        ),
    },
    "kicking": {
        "fieldGoalsMade/fieldGoalAttempts": (
            "field_goals_made",
            "field_goals_attempted",
        ),
        "extraPointsMade/extraPointAttempts": (
            "extra_points_made",
            "extra_points_attempted",
        ),
    },
}

FRACTIONAL = {"tackles_total", "sacks"}


def numeric(value, field):
    if value is None:
        return None

    text = str(value).strip().replace(",", "")

    if text in {"", "-", "--", "—", "N/A"}:
        return None

    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(
            f"Invalid {field} value: {value}"
        ) from exc

    if not result.is_finite():
        raise ValueError(f"Non-finite {field} value")

    if field in FRACTIONAL:
        return result

    if result != result.to_integral_value():
        raise ValueError(
            f"Expected integer for {field}: {value}"
        )

    return int(result)


def parse_stats(payload, game_id, expected_teams):
    header = payload.get("header") or {}

    if str(header.get("id")) != str(game_id):
        raise ValueError("Box score event ID mismatch")

    competitions = header.get("competitions") or []

    if not competitions:
        raise ValueError("Missing competition header")

    completed = (
        competitions[0].get("status") or {}
    ).get("type", {}).get("completed")

    if completed is not True:
        raise ValueError("Game is not confirmed final")

    teams = (payload.get("boxscore") or {}).get("players")

    if not isinstance(teams, list):
        raise ValueError("Missing player box scores")

    received_teams = [
        str((team.get("team") or {}).get("id"))
        for team in teams
    ]

    if (
        len(received_teams) != 2
        or set(received_teams) != set(expected_teams)
    ):
        raise ValueError(
            "Box score teams do not match saved game"
        )

    players = {}

    for team in teams:
        team_id = str(team["team"]["id"])
        team_count = 0

        for category in team.get("statistics") or []:
            category_name = category.get("name")

            if (
                category_name not in MAPS
                and category_name not in PAIRS
            ):
                continue

            keys = category.get("keys")
            athletes = category.get("athletes")

            if (
                not isinstance(keys, list)
                or not isinstance(athletes, list)
            ):
                raise ValueError("Malformed stat category")

            for entry in athletes:
                athlete = entry.get("athlete") or {}
                player_id = str(athlete.get("id") or "")
                name = athlete.get("displayName")
                values = entry.get("stats")

                if (
                    not player_id.isdigit()
                    or not name
                    or not isinstance(values, list)
                    or len(keys) != len(values)
                ):
                    raise ValueError("Malformed player stat row")

                if player_id in players:
                    if players[player_id]["team_id"] != team_id:
                        raise ValueError(
                            "Player appears on both teams"
                        )
                else:
                    players[player_id] = {
                        "id": player_id,
                        "name": name,
                        "team_id": team_id,
                        "headshot": (
                            athlete.get("headshot") or {}
                        ).get("href"),
                        "stats": {},
                    }

                saved = players[player_id]["stats"]
                source = dict(zip(keys, values))

                for source_key, field in MAPS.get(
                    category_name, {}
                ).items():
                    if source_key in source:
                        saved[field] = numeric(
                            source[source_key], field
                        )

                for source_key, fields in PAIRS.get(
                    category_name, {}
                ).items():
                    raw = source.get(source_key)

                    if raw is None or str(raw).strip() in {
                        "", "-", "--", "—", "N/A"
                    }:
                        continue

                    parts = str(raw).split("/")

                    if len(parts) != 2:
                        raise ValueError(
                            f"Invalid paired stat: {raw}"
                        )

                    first = numeric(parts[0], fields[0])
                    second = numeric(parts[1], fields[1])

                    if (
                        first is None
                        or second is None
                        or first < 0
                        or second < first
                    ):
                        raise ValueError(
                            f"Invalid paired stat: {raw}"
                        )

                    saved[fields[0]] = first
                    saved[fields[1]] = second

                team_count += 1

        if team_count == 0:
            raise ValueError(
                f"No supported stats for team {team_id}"
            )

    return [
        player
        for player in players.values()
        if any(
            value is not None
            for value in player["stats"].values()
        )
    ]


def save_game(game_id, players):
    columns = ", ".join(FIELDS)
    placeholders = ", ".join(["%s"] * (3 + len(FIELDS)))

    updates = ", ".join(
        f"{field} = EXCLUDED.{field}"
        for field in FIELDS
    )

    # Column names come only from the fixed FIELDS constant.
    query = f"""
        INSERT INTO public.nfl_player_game_stats (
            game_id, player_id, team_id, {columns}
        )
        VALUES ({placeholders})
        ON CONFLICT (game_id, player_id) DO UPDATE SET
            team_id = EXCLUDED.team_id,
            {updates},
            updated_at = NOW()
    """

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            # Historical games must not overwrite current teams
            # or reactivate players who left a roster.
            cursor.executemany(
                """
                INSERT INTO public.nfl_players (
                    id, name, headshot_url, is_active
                )
                VALUES (%s, %s, %s, FALSE)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    headshot_url = COALESCE(
                        EXCLUDED.headshot_url,
                        nfl_players.headshot_url
                    )
                """,
                [
                    (
                        player["id"],
                        player["name"],
                        player["headshot"],
                    )
                    for player in players
                ],
            )

            cursor.executemany(
                query,
                [
                    (
                        str(game_id),
                        player["id"],
                        player["team_id"],
                        *[
                            player["stats"].get(field)
                            for field in FIELDS
                        ],
                    )
                    for player in players
                ],
            )


def main():
    season = int(
        os.environ.get("NFL_SEASON") or current_season()
    )

    batch_size = max(
        1,
        min(
            int(os.environ.get("NFL_STATS_BATCH", "30")),
            100,
        ),
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
                    g.id,
                    g.home_team_id,
                    g.away_team_id
                FROM public.nfl_games g
                WHERE g.season = %s
                  AND g.status = 'final'
                  AND g.season_type IN (2, 3)
                  AND (
                      NOT EXISTS (
                          SELECT 1
                          FROM public.nfl_player_game_stats s
                          WHERE s.game_id = g.id
                      )
                      OR (
                          g.starts_at >= NOW() - INTERVAL '7 days'
                          AND EXISTS (
                              SELECT 1
                              FROM public.nfl_player_game_stats s
                              WHERE s.game_id = g.id
                              GROUP BY s.game_id
                              HAVING MIN(s.updated_at)
                                  < NOW() - INTERVAL '6 hours'
                          )
                      )
                  )
                ORDER BY
                    NOT EXISTS (
                        SELECT 1
                        FROM public.nfl_player_game_stats s
                        WHERE s.game_id = g.id
                    ) DESC,
                    g.starts_at DESC
                LIMIT %s
                """,
                (season, batch_size),
            )

            games = cursor.fetchall()

    failed = []
    imported = 0

    for game_id, home_id, away_id in games:
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
            imported += 1

            print(
                f"{game_id}: "
                f"{len(players)} player box scores saved",
                flush=True,
            )

        except Exception as exc:
            failed.append(str(game_id))
            print(
                f"{game_id}: FAILED — {exc}",
                flush=True,
            )

    print(f"Games imported: {imported}")
    print(f"Games failed: {len(failed)}")

    if failed:
        raise RuntimeError(
            "These games need another attempt: "
            + ", ".join(failed)
        )


if __name__ == "__main__":
    main()
