"""Collect game-specific injuries and confirmed/projected starters."""

import os

from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from backfill_season import fetch, get_teams


BLOCKED = {"Out", "Doubtful"}
POSITIONS = ("pg", "sg", "sf", "pf", "c")


def team_code(value):
    code = str(value or "").upper()

    return {
        "GS": "GSW",
        "NY": "NYK",
        "SA": "SAS",
        "NO": "NOP",
        "UTAH": "UTA",
        "WSH": "WAS",
    }.get(code, code)


def athlete_id(athlete):
    raw = str(athlete.get("id") or "")

    return -int(raw) if raw.isdigit() else None


def injury_status(injury):
    fantasy = (
        (injury.get("details") or {}).get("fantasyStatus")
        or {}
    )

    values = {
        str(value or "").strip().upper()
        for value in (
            injury.get("status"),
            fantasy.get("description"),
            fantasy.get("abbreviation"),
        )
    }

    options = (
        (
            {
                "OUT", "O", "IR",
                "INJURED RESERVE", "SUSPENDED",
            },
            "Out",
        ),
        (
            {"DOUBTFUL", "D", "DD"},
            "Doubtful",
        ),
        (
            {
                "QUESTIONABLE", "Q", "GTD",
                "DAY-TO-DAY", "DTD",
            },
            "Questionable",
        ),
        (
            {"PROBABLE", "P"},
            "Probable",
        ),
    )

    for aliases, label in options:
        if values & aliases:
            return label

    return str(
        injury.get("status") or "Unknown"
    )


def injury_map(payload, abbreviation):
    found = {}

    for group in payload.get("injuries") or []:
        code = str(
            (group.get("team") or {}).get("abbreviation")
            or ""
        ).upper()

        if team_code(code) != team_code(abbreviation):
            continue

        for injury in group.get("injuries") or []:
            pid = athlete_id(
                injury.get("athlete") or {}
            )

            if pid is not None:
                status = injury_status(injury)

                if found.get(pid) != "Out":
                    found[pid] = status

    return found


def resolve_statuses(
    payload,
    abbreviation,
    eligible,
    previous,
    announced=(),
):
    fresh = injury_map(
        payload,
        abbreviation,
    )

    report_present = False

    for group in payload.get("injuries") or []:
        code = (
            group.get("team") or {}
        ).get("abbreviation")

        injuries = group.get("injuries")

        if team_code(code) != team_code(abbreviation):
            continue

        if isinstance(injuries, list) and all(
            isinstance(item, dict)
            and athlete_id(
                item.get("athlete") or {}
            ) is not None
            for item in injuries
        ):
            report_present = True

    resolved = {}

    for pid in eligible:
        status = fresh.get(pid)

        if status in {
            "Out",
            "Doubtful",
            "Questionable",
            "Probable",
        }:
            resolved[pid] = status

        elif pid in announced or (
            pid not in fresh and report_present
        ):
            resolved[pid] = "Not listed"

        else:
            resolved[pid] = previous.get(pid)

    return resolved


def confirmed_starters(
    payload,
    abbreviation,
    eligible,
):
    groups = (
        (payload.get("boxscore") or {}).get("players")
        or []
    )

    for group in groups:
        code = str(
            (group.get("team") or {}).get("abbreviation")
            or ""
        ).upper()

        if team_code(code) != team_code(abbreviation):
            continue

        starters = set()

        for category in group.get("statistics") or []:
            for item in category.get("athletes") or []:
                pid = athlete_id(
                    item.get("athlete") or {}
                )

                if (
                    item.get("starter") is True
                    and pid in eligible
                ):
                    starters.add(pid)

        if len(starters) == 5:
            return starters

    return None


def projected_starters(
    payload,
    eligible,
    statuses,
):
    for chart in payload.get("depthchart") or []:
        positions = chart.get("positions") or {}
        chosen = set()

        for position in POSITIONS:
            athletes = (
                (positions.get(position) or {}).get("athletes")
                or []
            )

            for athlete in athletes:
                pid = athlete_id(athlete)

                if (
                    pid in eligible
                    and pid not in chosen
                    and statuses.get(pid) not in BLOCKED
                ):
                    chosen.add(pid)
                    break

        if len(chosen) == 5:
            return chosen

    return None


def main():
    today = datetime.now(
        ZoneInfo("America/Chicago")
    ).date()

    directory = fetch("/teams")

    if directory is None:
        raise RuntimeError(
            "ESPN team directory unavailable; try again."
        )

    failed = []
    total = 0

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            teams = get_teams(cursor)

            if "WAS" in teams:
                teams["WSH"] = teams["WAS"]

            espn = {}

            for sport in directory.get("sports") or []:
                for league in sport.get("leagues") or []:
                    for entry in league.get("teams") or []:
                        item = entry.get("team") or {}

                        code = str(
                            item.get("abbreviation") or ""
                        ).upper()

                        if code in teams and item.get("id"):
                            espn[teams[code]] = (
                                str(item["id"]),
                                code,
                            )

            cursor.execute(
                """
                SELECT
                    id,
                    home_team_id,
                    away_team_id
                FROM games
                WHERE game_date = %s
                ORDER BY id
                """,
                (today,),
            )

            games = cursor.fetchall()

            cursor.execute(
                """
                SELECT
                    game_id,
                    player_id,
                    team_id,
                    injury_status
                FROM game_player_availability
                WHERE game_id = ANY(%s::text[])
                """,
                ([str(gid) for gid, _, _ in games],),
            )

            previous_statuses = {
                (str(gid), pid, tid): status
                for gid, pid, tid, status in cursor.fetchall()
            }

            cursor.execute(
                """
                SELECT DISTINCT ON (player_id)
                    player_id,
                    injury_status
                FROM game_player_availability
                WHERE injury_status IS NOT NULL
                ORDER BY
                    player_id,
                    checked_at DESC,
                    game_id DESC
                """
            )

            latest_statuses = dict(
                cursor.fetchall()
            )

            cursor.execute(
                """
                SELECT id, current_team_id
                FROM players
                WHERE is_active = TRUE
                """
            )

            players = {}

            for pid, tid in cursor.fetchall():
                players.setdefault(tid, set()).add(pid)

            connection.commit()

            for gid, home_id, away_id in games:
                summary = fetch(
                    "/summary",
                    {"event": str(gid)},
                )

                if summary is None:
                    failed.append(str(gid))
                    continue

                for tid in (away_id, home_id):
                    if tid not in espn:
                        failed.append(
                            f"{gid}: unmapped team {tid}"
                        )
                        continue

                    espn_id, code = espn[tid]
                    eligible = players.get(tid, set())

                    if not eligible:
                        failed.append(
                            f"{gid}: {code} roster not collected yet"
                        )
                        continue

                    try:
                        previous = {
                            pid: (
                                previous_statuses.get(
                                    (str(gid), pid, tid)
                                )
                                or latest_statuses.get(pid)
                            )
                            for pid in eligible
                        }

                        announced = confirmed_starters(
                            summary,
                            code,
                            eligible,
                        ) or ()

                        statuses = resolve_statuses(
                            summary,
                            code,
                            eligible,
                            previous,
                            announced,
                        )

                        lineup_eligible = {
                            pid
                            for pid in eligible
                            if statuses.get(pid) not in BLOCKED
                        }

                        starters = confirmed_starters(
                            summary,
                            code,
                            lineup_eligible,
                        )

                        source = (
                            "confirmed"
                            if starters is not None
                            else None
                        )

                        if starters is None:
                            depth = fetch(
                                f"/teams/{espn_id}/depthcharts"
                            )

                            if depth is not None:
                                starters = projected_starters(
                                    depth,
                                    eligible,
                                    statuses,
                                )

                                if starters is not None:
                                    source = "projected"

                        for pid in eligible:
                            status = statuses.get(pid)

                            cursor.execute(
                                """
                                INSERT INTO game_player_availability (
                                    game_id,
                                    player_id,
                                    team_id,
                                    injury_status,
                                    is_starter,
                                    starter_source,
                                    checked_at
                                )
                                VALUES (
                                    %s, %s, %s, %s,
                                    %s, %s, NOW()
                                )
                                ON CONFLICT (game_id, player_id)
                                DO UPDATE SET
                                    team_id = EXCLUDED.team_id,
                                    injury_status = COALESCE(
                                        EXCLUDED.injury_status,
                                        game_player_availability.injury_status
                                    ),
                                    is_starter = EXCLUDED.is_starter,
                                    starter_source = EXCLUDED.starter_source,
                                    checked_at = NOW()
                                """,
                                (
                                    str(gid),
                                    pid,
                                    tid,
                                    status,
                                    (
                                        pid in starters
                                        if starters is not None
                                        else None
                                    ),
                                    source,
                                ),
                            )

                        connection.commit()
                        total += len(eligible)

                        print(
                            f"{gid} {code}: "
                            f"{len(eligible)} players checked; "
                            f"lineup {source or 'unavailable'}",
                            flush=True,
                        )

                    except Exception as error:
                        connection.rollback()

                        failed.append(
                            f"{gid}: {code}"
                        )

                        print(
                            f"{gid} {code}: update failed: {error}",
                            flush=True,
                        )

    print(
        f"Game-player records refreshed: {total}",
        flush=True,
    )

    if failed:
        raise RuntimeError(
            "Retry missing updates: " + ", ".join(failed)
        )


if __name__ == "__main__":
    main()
