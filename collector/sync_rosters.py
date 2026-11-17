"""Refresh current NBA rosters without changing historical box scores."""

import os

import psycopg
from backfill_season import fetch, get_teams


ALIASES = {
    "GS": "GSW",
    "NY": "NYK",
    "SA": "SAS",
    "NO": "NOP",
    "UTAH": "UTA",
    "WSH": "WAS",
}


def roster_entries(payload):
    athletes = payload.get("athletes")

    if not isinstance(athletes, list):
        raise ValueError("Roster athlete list is missing")

    for entry in athletes:
        if not isinstance(entry, dict):
            raise ValueError("Invalid roster entry")

        items = (
            entry.get("items")
            if "items" in entry
            else [entry]
        )

        if not isinstance(items, list):
            raise ValueError("Invalid roster group")

        for athlete in items:
            if not isinstance(athlete, dict):
                raise ValueError("Invalid athlete")

            yield athlete


def collect_snapshot(directory, teams):
    mapped = {}

    for sport in directory.get("sports") or []:
        for league in sport.get("leagues") or []:
            for entry in league.get("teams") or []:
                team = entry.get("team") or {}

                code = str(
                    team.get("abbreviation") or ""
                ).upper()

                tid = teams.get(
                    ALIASES.get(code, code)
                )

                if tid is None:
                    tid = teams.get(code)

                if tid is not None and team.get("id"):
                    mapped[tid] = (
                        str(team["id"]),
                        code,
                    )

    if len(mapped) != 30:
        raise RuntimeError(
            "All 30 teams must be mapped; saved rosters retained"
        )

    snapshot = {}
    failed = []

    for tid, (espn_id, code) in mapped.items():
        try:
            roster = fetch(
                f"/teams/{espn_id}/roster"
            )

            if roster is None:
                raise ValueError("Roster unavailable")

            response_id = (
                roster.get("team") or {}
            ).get("id")

            if response_id and str(response_id) != espn_id:
                raise ValueError(
                    "Roster team ID does not match"
                )

            rows = {}

            for athlete in roster_entries(roster):
                raw = str(
                    athlete.get("id") or ""
                )

                name = (
                    athlete.get("displayName")
                    or athlete.get("fullName")
                )

                if not raw.isdigit() or not name:
                    raise ValueError(
                        "Athlete ID or name is missing"
                    )

                pid = -int(raw)

                if (
                    pid in snapshot
                    and snapshot[pid][2] != tid
                ):
                    raise ValueError(
                        "Player listed on two teams; retry later"
                    )

                position = (
                    athlete.get("position") or {}
                ).get("abbreviation")

                rows[pid] = (
                    pid,
                    name,
                    tid,
                    position or None,
                    athlete.get("displayHeight") or None,
                    athlete.get("displayWeight") or None,
                )

            if not 8 <= len(rows) <= 35:
                raise ValueError(
                    "Roster size looks incomplete or invalid"
                )

            snapshot.update(rows)

            print(
                f"{code}: {len(rows)} roster players checked",
                flush=True,
            )

        except Exception as error:
            failed.append(f"{code}: {error}")

    if failed:
        raise RuntimeError(
            "No roster changes saved. Retry: "
            + "; ".join(failed)
        )

    return list(snapshot.values())


def main():
    directory = fetch("/teams")

    if directory is None:
        raise RuntimeError(
            "Team directory unavailable; saved rosters retained"
        )

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            teams = get_teams(cursor)
            connection.commit()

            rows = collect_snapshot(
                directory,
                teams,
            )

            ids = [row[0] for row in rows]

            cursor.execute(
                """
                SELECT id, current_team_id
                FROM players
                WHERE id = ANY(%s::bigint[])
                """,
                (ids,),
            )

            previous = dict(cursor.fetchall())

            changes = [
                (name, previous[pid], tid)
                for pid, name, tid, *_ in rows
                if (
                    previous.get(pid) is not None
                    and previous[pid] != tid
                )
            ]

            cursor.executemany(
                """
                INSERT INTO players (
                    id,
                    full_name,
                    current_team_id,
                    listed_position,
                    listed_height,
                    listed_weight,
                    is_active,
                    updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    TRUE, NOW()
                )
                ON CONFLICT (id) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    current_team_id = EXCLUDED.current_team_id,
                    listed_position = CASE
                        WHEN EXCLUDED.listed_position IS NULL
                            THEN players.listed_position
                        WHEN EXCLUDED.listed_position IN (
                            'G', 'F', 'G-F',
                            'F-G', 'F-C', 'C-F'
                        )
                        AND players.listed_position IS NOT NULL
                            THEN players.listed_position
                        ELSE EXCLUDED.listed_position
                    END,
                    listed_height = COALESCE(
                        EXCLUDED.listed_height,
                        players.listed_height
                    ),
                    listed_weight = COALESCE(
                        EXCLUDED.listed_weight,
                        players.listed_weight
                    ),
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                rows,
            )

            cursor.execute(
                """
                UPDATE players
                SET is_active = FALSE,
                    current_team_id = NULL,
                    updated_at = NOW()
                WHERE is_active = TRUE
                  AND NOT (id = ANY(%s::bigint[]))
                """,
                (ids,),
            )

            removed = cursor.rowcount
            connection.commit()

    print(
        f"Current roster players saved: {len(rows)}",
        flush=True,
    )

    print(
        f"No longer on an NBA roster: {removed}",
        flush=True,
    )

    print(
        f"Team changes detected: {len(changes)}",
        flush=True,
    )

    for name, old, new in changes:
        print(
            f"{name}: team {old} -> team {new}",
            flush=True,
        )


if __name__ == "__main__":
    main()
