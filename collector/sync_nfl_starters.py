"""Save projected NFL offensive starters, kickers, and punters."""

import os

import psycopg

from sync_nfl_rosters import BASE, fetch


BLOCKED = {
    "out",
    "doubtful",
    "injured reserve",
    "suspended",
    "pup",
    "nfi",
    "practice squad",
}


def unavailable(injury_status, roster_status):
    values = {
        str(injury_status or "").strip().lower(),
        str(roster_status or "").strip().lower(),
    }

    return bool(values & BLOCKED)


def select_starters(payload, eligible):
    charts = payload.get("depthchart")

    if not isinstance(charts, list) or not charts:
        raise ValueError("No NFL depth chart returned")

    offense = None
    special_teams = []

    for chart in charts:
        positions = chart.get("positions")

        if not isinstance(positions, dict):
            continue

        normalized = {
            str(key).lower(): value
            for key, value in positions.items()
        }

        if offense is None and "qb" in normalized:
            offense = normalized

        for key in ("pk", "k", "p"):
            if key in normalized:
                special_teams.append(normalized[key])

    if offense is None:
        raise ValueError(
            "No offensive depth chart with a QB slot"
        )

    starters = set()

    for slot in [*offense.values(), *special_teams]:
        athletes = slot.get("athletes")

        if not isinstance(athletes, list):
            raise ValueError(
                "Invalid depth-chart athlete list"
            )

        for athlete in athletes:
            pid = str(athlete.get("id") or "")

            if pid in eligible and pid not in starters:
                starters.add(pid)
                break

    return starters


def main():
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, home_team_id, away_team_id
                FROM public.nfl_games
                WHERE starts_at >= NOW()
                  AND starts_at < NOW() + INTERVAL '14 days'
                  AND season_type IN (2, 3)
                  AND status IN (
                      'scheduled', 'postponed', 'delayed'
                  )
                ORDER BY starts_at
                """
            )

            games = cursor.fetchall()

            cursor.execute(
                """
                SELECT id, current_team_id
                FROM public.nfl_players
                WHERE is_active = TRUE
                  AND current_team_id IS NOT NULL
                """
            )

            players_by_team = {}

            for pid, team_id in cursor.fetchall():
                players_by_team.setdefault(
                    str(team_id), set()
                ).add(str(pid))

            cursor.execute(
                """
                SELECT
                    game_id,
                    player_id,
                    team_id,
                    injury_status,
                    roster_status
                FROM public.nfl_player_availability
                """
            )

            availability = {
                (
                    str(game_id),
                    str(pid),
                    str(team_id),
                ): (injury, roster)
                for game_id, pid, team_id, injury, roster
                in cursor.fetchall()
            }

    if not games:
        print("No upcoming NFL games to project.")
        return

    team_ids = {
        str(team_id)
        for _, home_id, away_id in games
        for team_id in (home_id, away_id)
    }

    charts = {}
    failures = []

    for team_id in sorted(team_ids):
        try:
            payload = fetch(
                f"{BASE}/teams/{team_id}/depthcharts"
            )

            select_starters(payload, set())
            charts[team_id] = payload

        except Exception as exc:
            failures.append(team_id)

            print(
                f"Team {team_id}: depth chart failed — {exc}",
                flush=True,
            )

    rows = []

    for game_id, home_id, away_id in games:
        game_id = str(game_id)

        for team_id in (str(home_id), str(away_id)):
            if team_id not in charts:
                continue

            current_players = players_by_team.get(
                team_id, set()
            )

            if not current_players:
                failures.append(team_id)
                continue

            eligible = set()
            checked_count = 0

            for pid in current_players:
                key = (game_id, pid, team_id)

                if key not in availability:
                    continue

                checked_count += 1
                injury, roster = availability[key]

                if not unavailable(injury, roster):
                    eligible.add(pid)

            if checked_count == 0:
                failures.append(team_id)

                print(
                    f"Team {team_id}: no availability records "
                    f"for game {game_id}. "
                    "Existing projections unchanged.",
                    flush=True,
                )

                continue

            starters = select_starters(
                charts[team_id],
                eligible,
            )

            for pid in current_players:
                is_starter = pid in starters

                rows.append(
                    (
                        game_id,
                        pid,
                        team_id,
                        is_starter,
                        "projected" if is_starter else None,
                        "ESPN depth chart",
                    )
                )

            print(
                f"Game {game_id}, team {team_id}: "
                f"{len(starters)} projected offensive/"
                "kicking/punting starters",
                flush=True,
            )

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO public.nfl_player_availability (
                    game_id,
                    player_id,
                    team_id,
                    is_starter,
                    starter_source,
                    source
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (game_id, player_id) DO UPDATE SET
                    is_starter = EXCLUDED.is_starter,
                    starter_source = EXCLUDED.starter_source
                WHERE nfl_player_availability.team_id
                    = EXCLUDED.team_id
                  AND nfl_player_availability.starter_source
                    IS DISTINCT FROM 'confirmed'
                """,
                rows,
            )

    print(f"Starter records processed: {len(rows)}")
    print("Injury statuses and NBA data unchanged.")

    if failures:
        raise RuntimeError(
            "Retry teams with missing depth charts "
            "or availability: "
            + ", ".join(sorted(set(failures)))
        )


if __name__ == "__main__":
    main()
