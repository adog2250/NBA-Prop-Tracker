"""Import NFL box scores, including verified zero-stat appearances."""

import os
import re
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

    if not isinstance(teams, list) or len(teams) != 2:
        raise ValueError("Missing two-team player box scores")

    received_teams = {
        str((team.get("team") or {}).get("id"))
        for team in teams
    }

    if received_teams != set(expected_teams):
        raise ValueError(
            "Box score teams do not match saved game"
        )

    players = {}

    for team in teams:
        team_id = str(team["team"]["id"])
        categories = {}
        played = set()
        dnp = set()

        for category in team.get("statistics") or []:
            category_name = category.get("name")

            supported = (
                category_name in MAPS
                or category_name in PAIRS
            )

            participation_only = category_name in {
                "kickReturns",
                "puntReturns",
                "punting",
            }

            if not supported and not participation_only:
                continue

            keys = category.get("keys")
            athletes = category.get("athletes")

            if (
                not isinstance(keys, list)
                or not isinstance(athletes, list)
            ):
                raise ValueError("Malformed stat category")

            mapping = MAPS.get(category_name, {})
            pairs = PAIRS.get(category_name, {})

            required = set(mapping) | set(pairs)
            complete = required.issubset(keys)
            present = set()
            fields = set(mapping.values())

            for pair in pairs.values():
                fields.update(pair)

            for entry in athletes:
                athlete = entry.get("athlete") or {}
                pid = str(athlete.get("id") or "")

                if not pid.isdigit():
                    raise ValueError("Missing player ID")

                if (
                    entry.get("didNotPlay") is True
                    or athlete.get("didNotPlay") is True
                ):
                    dnp.add(pid)
                    continue

                values = entry.get("stats")
                player_name = athlete.get("displayName")

                if (
                    not player_name
                    or not isinstance(values, list)
                    or len(keys) != len(values)
                ):
                    raise ValueError("Malformed player stat row")

                source = dict(zip(keys, values))
                parsed = {}

                for source_key, field in mapping.items():
                    parsed[field] = numeric(
                        source.get(source_key),
                        field,
                    )

                    if parsed[field] is None:
                        complete = False

                for source_key, pair in pairs.items():
                    raw = source.get(source_key)

                    if raw is None or str(raw).strip() in {
                        "", "-", "--", "—", "N/A"
                    }:
                        parsed.update({
                            field: None
                            for field in pair
                        })

                        complete = False
                        continue

                    parts = str(raw).split("/")

                    if len(parts) != 2:
                        raise ValueError(
                            f"Invalid paired stat: {raw}"
                        )

                    first = numeric(parts[0], pair[0])
                    second = numeric(parts[1], pair[1])

                    if (
                        first is None
                        or second is None
                        or first < 0
                        or second < first
                    ):
                        raise ValueError(
                            f"Invalid paired stat: {raw}"
                        )

                    parsed[pair[0]] = first
                    parsed[pair[1]] = second

                if supported:
                    appeared = any(
                        value is not None
                        for value in parsed.values()
                    )
                else:
                    # A recorded return or punting line can
                    # establish a player appearance.
                    appeared = any(
                        re.fullmatch(
                            r"-?\d+(?:\.\d+)?",
                            str(value).strip(),
                        )
                        for value in values
                    )

                if not appeared:
                    continue

                if (
                    pid in players
                    and players[pid]["team_id"] != team_id
                ):
                    raise ValueError(
                        "Player appears on both teams"
                    )

                player = players.setdefault(pid, {
                    "id": pid,
                    "name": player_name,
                    "team_id": team_id,
                    "headshot": (
                        athlete.get("headshot") or {}
                    ).get("href"),
                    "stats": {},
                })

                player["stats"].update(parsed)
                played.add(pid)
                present.add(pid)

            if supported:
                if category_name in categories:
                    raise ValueError(
                        f"Duplicate stat category: {category_name}"
                    )

                categories[category_name] = (
                    fields,
                    present,
                    complete,
                )

        played -= dnp

        if not played:
            raise ValueError(
                f"No verified player appearances "
                f"for team {team_id}"
            )

        for pid in played:
            for fields, present, complete in categories.values():
                # A player with a verified appearance who is
                # absent from a complete stat category had no
                # recorded events in that category.
                #
                # Example: a receiver with catches but no
                # rushing entry receives zero rushing stats.
                #
                # Explicit unavailable cells and entirely
                # missing categories are not converted to zero.
                if complete and pid not in present:
                    for field in fields:
                        players[pid]["stats"].setdefault(
                            field, 0
                        )

        for pid in dnp:
            players.pop(pid, None)

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
            # Historical games do not change current teams
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
