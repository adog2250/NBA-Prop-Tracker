"""Save ESPN depth-chart positions after the roster backfill."""

import os

import psycopg

from backfill_season import fetch, get_teams


POSITIONS = ("PG", "SG", "SF", "PF", "C")


def player_positions(payload):
    """Collect listed roles, ordered by depth rank then position."""
    roles = {}

    for chart in payload.get("depthchart") or []:
        for key, group in (chart.get("positions") or {}).items():
            position = key.upper()

            if position not in POSITIONS:
                continue

            for rank, athlete in enumerate(group.get("athletes") or []):
                raw_id = str(athlete.get("id") or "")

                if not raw_id.isdigit():
                    continue

                player_id = -int(raw_id)
                ranks = roles.setdefault(player_id, {})
                ranks[position] = min(
                    rank,
                    ranks.get(position, rank),
                )

    return {
        player_id: " / ".join(
            sorted(
                ranks,
                key=lambda position: (
                    ranks[position],
                    POSITIONS.index(position),
                ),
            )
        )
        for player_id, ranks in roles.items()
    }


def main():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL secret is missing.")

    directory = fetch("/teams")

    if directory is None:
        raise RuntimeError("ESPN team directory could not load.")

    failed = []
    processed = set()
    updated = 0

    with psycopg.connect(
        database_url,
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            teams = get_teams(cursor)

            for sport in directory.get("sports") or []:
                for league in sport.get("leagues") or []:
                    for entry in league.get("teams") or []:
                        team = entry.get("team") or {}

                        abbreviation = str(
                            team.get("abbreviation") or ""
                        ).upper()

                        team_id = teams.get(abbreviation)
                        espn_id = team.get("id")

                        if (
                            team_id is None
                            or not espn_id
                            or team_id in processed
                        ):
                            continue

                        processed.add(team_id)

                        payload = fetch(
                            f"/teams/{espn_id}/depthcharts"
                        )

                        positions = (
                            player_positions(payload)
                            if payload
                            else {}
                        )

                        if not positions:
                            failed.append(abbreviation)
                            continue

                        for player_id, position in positions.items():
                            cursor.execute(
                                """
                                UPDATE players
                                SET listed_position = %s,
                                    updated_at = NOW()
                                WHERE id = %s
                                  AND current_team_id = %s
                                  AND listed_position
                                      IS DISTINCT FROM %s
                                """,
                                (
                                    position,
                                    player_id,
                                    team_id,
                                    position,
                                ),
                            )

                            updated += cursor.rowcount

                        connection.commit()

                        print(
                            f"{abbreviation}: "
                            f"{len(positions)} player positions checked",
                            flush=True,
                        )

    print(
        f"Specific positions updated: {updated}",
        flush=True,
    )

    if len(processed) != 30 or failed:
        raise RuntimeError(
            f"Checked {len(processed)}/30 teams. "
            "Retry missing depth charts: "
            + (
                ", ".join(failed)
                or "incomplete team directory"
            )
        )


if __name__ == "__main__":
    main()
