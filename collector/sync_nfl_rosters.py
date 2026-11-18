"""Refresh NFL teams and current rosters without changing NBA data."""

import json
import os
import time
from urllib.request import Request, urlopen

import psycopg


BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"


def fetch(url):
    last_error = None

    for attempt in range(4):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "ZenStats/1.0",
                    "Accept": "application/json",
                },
            )

            with urlopen(request, timeout=30) as response:
                return json.load(response)

        except Exception as exc:
            last_error = exc

            if attempt < 3:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"Could not download {url}") from last_error


def athlete_entries(payload):
    groups = payload.get("athletes")

    if not isinstance(groups, list):
        raise ValueError("Roster response has no athlete list")

    athletes = []

    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Invalid roster entry")

        if "items" in group:
            items = group["items"]

            if not isinstance(items, list):
                raise ValueError("Invalid roster group")

            athletes.extend(items)
        else:
            athletes.append(group)

    if not all(isinstance(player, dict) for player in athletes):
        raise ValueError("Invalid athlete data")

    return athletes


def collect_snapshot():
    directory = fetch(f"{BASE}/teams?limit=1000")
    entries = directory["sports"][0]["leagues"][0]["teams"]

    teams = {}
    players = {}

    for entry in entries:
        team = entry["team"]
        team_id = str(team["id"])

        if team_id in teams:
            raise ValueError(f"Duplicate team: {team_id}")

        logos = team.get("logos") or []
        logo = next(
            (
                item.get("href")
                for item in logos
                if isinstance(item, dict) and item.get("href")
            ),
            None,
        )

        teams[team_id] = (
            team_id,
            team["displayName"],
            team["abbreviation"],
            logo,
        )

    if len(teams) != 32:
        raise ValueError(
            f"Expected 32 NFL teams; received {len(teams)}. "
            "No database changes saved."
        )

    for team_id, team_row in teams.items():
        payload = fetch(f"{BASE}/teams/{team_id}/roster")
        response_team = str((payload.get("team") or {}).get("id", ""))

        if response_team != team_id:
            raise ValueError(
                f"Roster team mismatch for {team_row[2]}"
            )

        team_players = {}

        for athlete in athlete_entries(payload):
            player_id = str(athlete.get("id") or "")
            name = athlete.get("displayName") or athlete.get("fullName")

            if not player_id.isdigit() or not name:
                raise ValueError(
                    f"Incomplete player on {team_row[2]} roster"
                )

            position = athlete.get("position") or {}
            headshot = athlete.get("headshot") or {}

            row = (
                player_id,
                name,
                team_id,
                position.get("abbreviation"),
                athlete.get("displayHeight"),
                athlete.get("displayWeight"),
                headshot.get("href"),
            )

            if (
                player_id in players
                and players[player_id][2] != team_id
            ):
                raise ValueError(
                    f"{name} appears on multiple teams. "
                    "Retry after the source finishes updating."
                )

            team_players[player_id] = row

        if not 35 <= len(team_players) <= 150:
            raise ValueError(
                f"Unexpected roster size for {team_row[2]}: "
                f"{len(team_players)}. No changes saved."
            )

        players.update(team_players)

        print(
            f"{team_row[2]}: "
            f"{len(team_players)} roster players checked",
            flush=True,
        )

    return teams, players


def main():
    # Validate every roster before writing anything.
    teams, players = collect_snapshot()

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, current_team_id FROM public.nfl_players"
            )
            previous = dict(cursor.fetchall())

            cursor.executemany(
                """
                INSERT INTO public.nfl_teams (
                    id, name, abbreviation, logo_url
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    abbreviation = EXCLUDED.abbreviation,
                    logo_url = COALESCE(
                        EXCLUDED.logo_url,
                        nfl_teams.logo_url
                    ),
                    updated_at = NOW()
                """,
                list(teams.values()),
            )

            cursor.executemany(
                """
                INSERT INTO public.nfl_players (
                    id,
                    name,
                    current_team_id,
                    position,
                    height,
                    weight,
                    headshot_url,
                    is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    current_team_id = EXCLUDED.current_team_id,
                    position = COALESCE(
                        EXCLUDED.position,
                        nfl_players.position
                    ),
                    height = COALESCE(
                        EXCLUDED.height,
                        nfl_players.height
                    ),
                    weight = COALESCE(
                        EXCLUDED.weight,
                        nfl_players.weight
                    ),
                    headshot_url = COALESCE(
                        EXCLUDED.headshot_url,
                        nfl_players.headshot_url
                    ),
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                list(players.values()),
            )

            cursor.execute(
                """
                UPDATE public.nfl_players
                SET
                    is_active = FALSE,
                    current_team_id = NULL,
                    updated_at = NOW()
                WHERE
                    is_active = TRUE
                    AND NOT (id = ANY(%s::text[]))
                """,
                (list(players),),
            )

            removed_count = cursor.rowcount

        # The connection commits all changes together on success.

    changes = [
        row
        for player_id, row in players.items()
        if previous.get(player_id) is not None
        and previous[player_id] != row[2]
    ]

    print(f"Teams updated: {len(teams)}")
    print(f"Players updated: {len(players)}")
    print(f"Players no longer on listed rosters: {removed_count}")
    print(f"Team changes detected: {len(changes)}")

    for row in changes:
        old_team = previous[row[0]]
        new_team = teams[row[2]][2]

        print(
            f"Team change: {row[1]} — "
            f"team ID {old_team} to {new_team}"
        )


if __name__ == "__main__":
    main()
