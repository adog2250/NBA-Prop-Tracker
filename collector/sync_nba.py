
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from nba_api.stats.static import teams as nba_teams


BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"

TEAM_ALIASES = {
    "GS": "GSW",
    "NY": "NYK",
    "NO": "NOP",
    "SA": "SAS",
    "UTAH": "UTA",
    "WSH": "WAS",
    "PHO": "PHX",
    "BRK": "BKN",
}


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 NBAPropTracker/1.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def normalize_team(value):
    key = str(value or "").strip().upper()
    return TEAM_ALIASES.get(key, key)


def team_maps(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, abbreviation FROM teams")
        return {
            abbreviation: team_id
            for team_id, abbreviation in cur.fetchall()
        }


def sync_teams(conn):
    records = nba_teams.get_teams()

    if len(records) < 30:
        raise RuntimeError("Incomplete NBA team list")

    with conn.cursor() as cur:
        for team in records:
            cur.execute(
                """
                INSERT INTO teams (
                    id, abbreviation, name, city, updated_at
                )
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    abbreviation = EXCLUDED.abbreviation,
                    name = EXCLUDED.name,
                    city = EXCLUDED.city,
                    updated_at = NOW()
                """,
                (
                    team["id"],
                    team["abbreviation"],
                    team["full_name"],
                    team["city"],
                ),
            )

    return len(records)


def score(entry):
    value = entry.get("score")

    if value in (None, ""):
        return None

    return int(value)


def sync_games(conn, day):
    data = fetch_json(
        f"{BASE}/scoreboard?dates={day:%Y%m%d}&limit=100"
    )

    teams = team_maps(conn)
    processed = 0
    finals = []

    with conn.cursor() as cur:
        for event in data.get("events", []):
            competition = (
                event.get("competitions") or [{}]
            )[0]

            entries = {
                entry.get("homeAway"): entry
                for entry in competition.get("competitors", [])
            }

            home = entries.get("home")
            away = entries.get("away")

            if not home or not away:
                continue

            home_abbr = normalize_team(
                (home.get("team") or {}).get("abbreviation")
            )

            away_abbr = normalize_team(
                (away.get("team") or {}).get("abbreviation")
            )

            home_id = teams.get(home_abbr)
            away_id = teams.get(away_abbr)

            if home_id is None or away_id is None:
                print(
                    f"SKIP unknown teams: {away_abbr} @ {home_abbr}",
                    flush=True,
                )
                continue

            status_info = (
                (event.get("status") or {}).get("type") or {}
            )

            complete = bool(status_info.get("completed"))

            if complete:
                status = "final"
            elif status_info.get("state") == "in":
                status = "live"
            else:
                status = "scheduled"

            event_date = event.get("date")

            if not event_date:
                continue

            local_date = datetime.fromisoformat(
                event_date.replace("Z", "+00:00")
            ).astimezone(
                ZoneInfo("America/New_York")
            ).date()

            season = event.get("season") or {}

            season_value = (
                f"{season.get('year', day.year)}-"
                f"{season.get('type', 2)}"
            )

            game_id = str(event["id"])

            cur.execute(
                """
                INSERT INTO games (
                    id,
                    game_date,
                    season,
                    home_team_id,
                    away_team_id,
                    home_score,
                    away_score,
                    status,
                    stats_verified,
                    updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, FALSE, NOW()
                )
                ON CONFLICT (id) DO UPDATE SET
                    game_date = EXCLUDED.game_date,
                    season = EXCLUDED.season,
                    home_team_id = EXCLUDED.home_team_id,
                    away_team_id = EXCLUDED.away_team_id,
                    home_score = EXCLUDED.home_score,
                    away_score = EXCLUDED.away_score,
                    status = EXCLUDED.status,
                    updated_at = NOW()
                """,
                (
                    game_id,
                    local_date,
                    season_value,
                    home_id,
                    away_id,
                    score(home),
                    score(away),
                    status,
                ),
            )

            processed += 1

            if complete:
                finals.append(game_id)

    return processed, finals


def number(value):
    if value is None:
        return None

    if str(value).strip().upper() in (
        "", "--", "-", "DNP", "N/A"
    ):
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def minutes_value(value):
    if value is None:
        return None

    try:
        value = str(value).strip()

        if ":" in value:
            minutes, seconds = value.split(":", 1)

            return round(
                int(minutes) + int(seconds) / 60,
                3,
            )

        return float(value)

    except (TypeError, ValueError):
        return None


def label_key(label):
    return (
        str(label)
        .strip()
        .upper()
        .replace(" ", "")
        .replace("_", "")
    )


def pick(stats, *names):
    for name in names:
        if name in stats:
            return stats[name]

    return None


def made_shots(value):
    if value is None:
        return None

    text = str(value).strip()

    if "-" in text:
        text = text.split("-", 1)[0]

    return number(text)


def sync_boxscore(conn, game_id):
    summary = fetch_json(
        f"{BASE}/summary?event={game_id}"
    )

    sections = (
        (summary.get("boxscore") or {}).get("players") or []
    )

    if not sections:
        print(
            f"SKIP {game_id}: no box score available",
            flush=True,
        )
        return 0

    teams = team_maps(conn)
    rows = []
    skipped = {}

    for section in sections:
        team = section.get("team") or {}

        abbreviation = normalize_team(
            team.get("abbreviation")
        )

        team_id = teams.get(abbreviation)

        if team_id is None:
            raise RuntimeError(
                f"Unknown team {abbreviation} in game {game_id}"
            )

        for group in section.get("statistics") or []:
            labels = (
                group.get("labels")
                or group.get("names")
                or []
            )

            for item in group.get("athletes") or []:
                athlete = item.get("athlete") or {}

                athlete_id = athlete.get("id")

                full_name = (
                    athlete.get("displayName")
                    or athlete.get("fullName")
                )

                values = item.get("stats") or []

                if not athlete_id or not full_name:
                    key = "missing identity"
                    skipped[key] = skipped.get(key, 0) + 1
                    continue

                # FIX:
                # ESPN can provide a reason such as
                # "COACH'S DECISION" even for players
                # who actually played.
                #
                # Only skip actual DNP players.
                if item.get("didNotPlay") is True:
                    key = "actual DNP"
                    skipped[key] = skipped.get(key, 0) + 1
                    continue

                if not labels or not values:
                    key = "missing statistics"
                    skipped[key] = skipped.get(key, 0) + 1
                    continue

                stats = {
                    label_key(label): value
                    for label, value in zip(labels, values)
                }

                pts = number(
                    pick(stats, "PTS", "POINTS")
                )

                reb = number(
                    pick(stats, "REB", "REBOUNDS")
                )

                ast = number(
                    pick(stats, "AST", "ASSISTS")
                )

                mins = minutes_value(
                    pick(stats, "MIN", "MINUTES")
                )

                if any(
                    value is None
                    for value in (pts, reb, ast, mins)
                ):
                    key = "incomplete stat fields"
                    skipped[key] = skipped.get(key, 0) + 1
                    continue

                # ESPN supplies 3PT as made-attempted.
                threes = made_shots(
                    pick(
                        stats,
                        "3PT",
                        "3PM-A",
                        "3PM",
                        "3P",
                    )
                )

                steals = number(
                    pick(stats, "STL", "STEALS")
                )

                blocks = number(
                    pick(stats, "BLK", "BLOCKS")
                )

                turnovers = number(
                    pick(
                        stats,
                        "TO",
                        "TOV",
                        "TURNOVERS",
                    )
                )

                # Negative ESPN IDs prevent collisions
                # with existing NBA API player IDs.
                player_id = -int(athlete_id)

                rows.append(
                    (
                        game_id,
                        player_id,
                        team_id,
                        full_name,
                        mins,
                        pts,
                        reb,
                        ast,
                        threes,
                        steals,
                        blocks,
                        turnovers,
                    )
                )

    if skipped:
        print(
            f"BOX SCORE {game_id} skipped: {skipped}",
            flush=True,
        )

    if not rows:
        print(
            f"SKIP {game_id}: no playable player records",
            flush=True,
        )
        return 0

    with conn.cursor() as cur:
        for row in rows:
            (
                gid,
                pid,
                tid,
                name,
                mins,
                pts,
                reb,
                ast,
                threes,
                steals,
                blocks,
                turnovers,
            ) = row

            cur.execute(
                """
                INSERT INTO players (
                    id,
                    full_name,
                    current_team_id,
                    is_active,
                    updated_at
                )
                VALUES (%s, %s, %s, TRUE, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    updated_at = NOW()
                """,
                (pid, name, tid),
            )

            cur.execute(
                """
                INSERT INTO player_game_stats (
                    game_id,
                    player_id,
                    team_id,
                    minutes,
                    points,
                    rebounds,
                    assists,
                    threes_made,
                    steals,
                    blocks,
                    turnovers
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (game_id, player_id)
                DO UPDATE SET
                    team_id = EXCLUDED.team_id,
                    minutes = EXCLUDED.minutes,
                    points = EXCLUDED.points,
                    rebounds = EXCLUDED.rebounds,
                    assists = EXCLUDED.assists,
                    threes_made = EXCLUDED.threes_made,
                    steals = EXCLUDED.steals,
                    blocks = EXCLUDED.blocks,
                    turnovers = EXCLUDED.turnovers
                """,
                (
                    gid,
                    pid,
                    tid,
                    mins,
                    pts,
                    reb,
                    ast,
                    threes,
                    steals,
                    blocks,
                    turnovers,
                ),
            )

    # Do not claim verification until the
    # full team/player box score is reconciled.
    print(
        f"BOX SCORE {game_id}: saved {len(rows)} players",
        flush=True,
    )

    return len(rows)


def record_sync(
    conn,
    started,
    status,
    count=0,
    error=None,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_history (
                job_name,
                started_at,
                completed_at,
                status,
                records_processed,
                error_message
            )
            VALUES (%s, %s, NOW(), %s, %s, %s)
            """,
            (
                "nba_schedule_sync",
                started,
                status,
                count,
                error,
            ),
        )


def main():
    url = os.environ.get("DATABASE_URL")

    if not url:
        raise RuntimeError("DATABASE_URL is missing")

    started = datetime.now(timezone.utc)

    with psycopg.connect(
        url,
        connect_timeout=10,
        sslmode="require",
    ) as conn:

        try:
            team_count = sync_teams(conn)
            conn.commit()

            today = datetime.now(
                ZoneInfo("America/New_York")
            ).date()

            game_count = 0
            final_ids = set()

            # Previous 7 days plus today and
            # the next 3 days.
            for offset in range(-7, 4):
                day = today + timedelta(days=offset)

                count, finals = sync_games(conn, day)
                conn.commit()

                game_count += count
                final_ids.update(finals)

                print(
                    f"{day}: {count} games processed",
                    flush=True,
                )

            player_count = 0
            failed_games = []

            for game_id in sorted(final_ids):
                try:
                    count = sync_boxscore(conn, game_id)
                    conn.commit()

                    player_count += count

                    print(
                        f"{game_id}: {count} player rows",
                        flush=True,
                    )

                except Exception as exc:
                    conn.rollback()

                    failed_games.append(
                        f"{game_id}: {exc}"
                    )

                    print(
                        f"WARNING: {game_id}: {exc}",
                        flush=True,
                    )

            if failed_games:
                raise RuntimeError(
                    f"{len(failed_games)} box scores failed: "
                    + "; ".join(failed_games[:3])
                )

            record_sync(
                conn,
                started,
                "success",
                team_count + game_count + player_count,
            )

            conn.commit()

            print(
                f"SUCCESS: {team_count} teams, "
                f"{game_count} games, "
                f"{player_count} player rows",
                flush=True,
            )

        except Exception as exc:
            conn.rollback()

            try:
                record_sync(
                    conn,
                    started,
                    "failed",
                    error=str(exc)[:2000],
                )
                conn.commit()

            except Exception:
                conn.rollback()

            raise


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
