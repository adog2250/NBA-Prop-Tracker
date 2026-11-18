"""Refresh upcoming NFL game availability without changing NBA data."""

import os
import re
from datetime import datetime, timezone

import psycopg

from sync_nfl_rosters import BASE, fetch


STATUS_NAMES = {
    "out": "Out",
    "o": "Out",
    "doubtful": "Doubtful",
    "d": "Doubtful",
    "questionable": "Questionable",
    "q": "Questionable",
    "day to day": "Questionable",
    "dtd": "Questionable",
    "gtd": "Questionable",
    "probable": "Probable",
    "active": "Available",
    "available": "Available",
    "healthy": "Available",
    "injured reserve": "Injured Reserve",
    "ir": "Injured Reserve",
    "reserve injured": "Injured Reserve",
    "suspended": "Suspended",
    "suspension": "Suspended",
    "physically unable to perform": "PUP",
    "pup": "PUP",
    "non football injury": "NFI",
    "nfi": "NFI",
}

ROSTER_BLOCKED = {
    "Injured Reserve",
    "Suspended",
    "PUP",
    "NFI",
    "Practice Squad",
}


def normalize_status(value):
    if isinstance(value, dict):
        values = [
            value.get("name"),
            value.get("description"),
            value.get("abbreviation"),
            value.get("type"),
        ]

        for item in values:
            result = normalize_status(item)

            if result is not None:
                return result

        return None

    key = str(value or "").strip().lower()
    key = re.sub(r"[-_/]+", " ", key)
    key = " ".join(key.split())

    return STATUS_NAMES.get(key)


def report_time(value):
    try:
        result = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result

    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def player_id(athlete):
    value = str(athlete.get("id") or "")

    if value.isdigit():
        return value

    # Some injury-feed athletes expose their ID in profile links.
    for link in athlete.get("links") or []:
        match = re.search(
            r"/id/(\d+)(?:/|$)",
            str(link.get("href") or ""),
        )

        if match:
            return match.group(1)

    return None


def injury_reports():
    payload = fetch(f"{BASE}/injuries")
    groups = payload.get("injuries")

    if not isinstance(groups, list):
        raise ValueError("NFL injury feed has no injury list")

    result = {}

    for group in groups:
        team_id = str(
            group.get("id")
            or (group.get("team") or {}).get("id")
            or ""
        )

        for report in group.get("injuries") or []:
            athlete = report.get("athlete") or {}
            pid = player_id(athlete)

            if pid is None:
                continue

            athlete_team = str(
                (athlete.get("team") or {}).get("id") or team_id
            )

            if athlete_team != team_id:
                continue

            result.setdefault((team_id, pid), []).append(report)

    return result


def roster_snapshot(team_id):
    payload = fetch(f"{BASE}/teams/{team_id}/roster")

    if str((payload.get("team") or {}).get("id")) != team_id:
        raise ValueError(f"Roster team mismatch: {team_id}")

    groups = payload.get("athletes")

    if not isinstance(groups, list):
        raise ValueError(f"Missing roster: {team_id}")

    result = {}

    for group in groups:
        items = group.get("items")

        if not isinstance(items, list):
            raise ValueError(f"Invalid roster group: {team_id}")

        group_name = re.sub(
            r"[^a-z]", "", str(group.get("position") or "").lower()
        )

        for athlete in items:
            pid = player_id(athlete)

            if pid is None:
                raise ValueError(f"Player ID missing: {team_id}")

            roster_status = None
            raw_status = athlete.get("status") or {}
            explicit_status = normalize_status(raw_status)

            if group_name == "practicesquad":
                roster_status = "Practice Squad"
            elif group_name == "suspended":
                roster_status = "Suspended"
            elif explicit_status in ROSTER_BLOCKED:
                roster_status = explicit_status
            elif explicit_status == "Available":
                roster_status = "Active"

            if pid in result:
                raise ValueError(
                    f"Duplicate player {pid} on team {team_id}"
                )

            result[pid] = {
                "roster_status": roster_status,
                "reports": athlete.get("injuries") or [],
            }

    if not 35 <= len(result) <= 150:
        raise ValueError(
            f"Unexpected roster size: team {team_id}, "
            f"{len(result)} players"
        )

    return result


def resolve_availability(snapshot, reports, previous):
    roster_status = snapshot["roster_status"]
    candidates = []

    for report in [*snapshot["reports"], *reports]:
        status = normalize_status(report.get("status"))

        if status is None:
            continue

        candidates.append(
            (
                report_time(report.get("date")),
                status,
                report.get("shortComment")
                or report.get("longComment"),
            )
        )

    if candidates:
        _, injury_status, description = max(
            candidates, key=lambda item: item[0]
        )
    else:
        # Missing reports do not clear a known injury.
        injury_status = previous.get("injury_status")
        description = previous.get("injury_description")

    if roster_status in ROSTER_BLOCKED:
        if roster_status != "Practice Squad":
            injury_status = roster_status

    # A generic Active roster label does not prove injury clearance.
    # It is deliberately stored separately from injury_status.
    return injury_status, description, roster_status


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
                  AND status IN ('scheduled', 'postponed', 'delayed')
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

            active = {}

            for pid, team_id in cursor.fetchall():
                active.setdefault(str(team_id), set()).add(str(pid))

            cursor.execute(
                """
                SELECT DISTINCT ON (player_id)
                    player_id,
                    injury_status,
                    injury_description,
                    roster_status
                FROM public.nfl_player_availability
                ORDER BY player_id, checked_at DESC, game_id DESC
                """
            )

            previous = {
                str(pid): {
                    "injury_status": injury,
                    "injury_description": description,
                    "roster_status": roster,
                }
                for pid, injury, description, roster
                in cursor.fetchall()
            }

    if not games:
        print("No upcoming NFL games in the next 14 days.")
        return

    reports = injury_reports()

    team_ids = {
        str(team_id)
        for _, home_id, away_id in games
        for team_id in (home_id, away_id)
    }

    # Download and validate all required rosters before writing.
    snapshots = {
        team_id: roster_snapshot(team_id)
        for team_id in sorted(team_ids)
    }

    rows = []
    blocked = 0

    for game_id, home_id, away_id in games:
        for team_id in (str(home_id), str(away_id)):
            snapshot = snapshots[team_id]

            for pid in sorted(active.get(team_id, set())):
                if pid not in snapshot:
                    # Never assign a player to a team whose current
                    # roster no longer lists them.
                    continue

                injury, description, roster = resolve_availability(
                    snapshot[pid],
                    reports.get((team_id, pid), []),
                    previous.get(pid, {}),
                )

                if (
                    injury in {
                        "Out", "Doubtful", "Injured Reserve",
                        "Suspended", "PUP", "NFI",
                    }
                    or roster in ROSTER_BLOCKED
                ):
                    blocked += 1

                rows.append(
                    (
                        str(game_id),
                        pid,
                        team_id,
                        injury,
                        description,
                        roster,
                        "ESPN injury feed and team roster",
                    )
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
                    injury_status,
                    injury_description,
                    roster_status,
                    source,
                    checked_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (game_id, player_id) DO UPDATE SET
                    team_id = EXCLUDED.team_id,
                    injury_status = EXCLUDED.injury_status,
                    injury_description = EXCLUDED.injury_description,
                    roster_status = EXCLUDED.roster_status,
                    source = EXCLUDED.source,
                    checked_at = NOW()
                """,
                rows,
            )

    print(f"Upcoming games checked: {len(games)}")
    print(f"Player/game availability records saved: {len(rows)}")
    print(f"Unavailable player/game records: {blocked}")
    print("NBA data unchanged.")


if __name__ == "__main__":
    main()
