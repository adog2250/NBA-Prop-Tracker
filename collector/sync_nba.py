
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from nba_api.stats.static import teams as nba_teams


BASE = (
    "https://site.api.espn.com/apis/site/v2/"
    "sports/basketball/nba"
)

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
            "User-Agent": "NBAPropTracker/1.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=25,
    ) as response:
        return json.load(response)


def normalize_team(value):
    value = str(value or "").strip().upper()
    return TEAM_ALIASES.get(value, value)


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


def team_maps(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, abbreviation FROM teams"
        )

        return {
            abbr: team_id
            for team_id, abbr in cur.fetchall()
        }


def score(entry):
    value = entry.get("score")

    if value in (None, ""):
        return None

    return int(value)


def sync_games(conn, day):
    url = (
        f"{BASE}/scoreboard"
        f"?dates={day:%Y%m%d}&limit=100"
    )

    data = fetch_json(url)
    teams = team_maps(conn)

    processed = 0
    finals = []

    with conn.cursor() as cur:
        for event in data.get("events", []):
            competitions = event.get(
                "competitions"
            ) or [{}]

            competition = competitions[0]

            entries = {
                entry.get("homeAway"): entry
                for entry in competition.get(
                    "competitors", []
                )
            }

            home = entries.get("home")
            away = entries.get("away")

            if not home or not away:
                continue

            home_abbr = normalize_team(
                home.get("team", {}).get(
                    "abbreviation"
                )
            )

            away_abbr = normalize_team(
                away.get("team", {}).get(
                    "abbreviation"
                )
            )

            home_id = teams.get(home_abbr)
            away_id = teams.get(away_abbr)

            if home_id is None or away_id is None:
                print(
                    f"SKIP unsupported game "
                    f"{event.get('id')}: "
                    f"{away_abbr} @ {home_abbr}",
                    flush=True,
                )
                continue

            status_info = (
                event.get("status", {})
                .get("type", {})
            )

            complete = bool(
                status_info.get("completed")
            )

            state = status_info.get(
                "state", "pre"
            )

            if complete:
                status = "final"
            elif state == "in":
                status = "live"
            else:
                status = "scheduled"

            event_date = event.get("date")

            if not event_date:
                raise RuntimeError(
                    f"Missing date for game "
                    f"{event.get('id')}"
                )

            local_date = datetime.fromisoformat(
                event_date.replace(
                    "Z", "+00:00"
                )
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

    value = str(value).strip()

    if value in (
        "", "--", "-", "DNP", "N/A"
    ):
        return None

    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def minutes_value(value):
    if value is None:
        return None

    value = str(value).strip()

    if ":" in value:
        try:
            mins, secs = value.split(
                ":", 1
            )

            return round(
                int(mins) + int(secs) / 60,
                3,
            )
        except (ValueError, TypeError):
            return None

    try:
        return float(value)
    except (ValueError, TypeError):
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


def sync_boxscore(conn, game_id):
    url = (
        f"{BASE}/summary?event={game_id}"
    )

    summary = fetch_json(url)

    sections = (
        (summary.get("boxscore") or {})
        .get("players") or []
    )

    if not sections:
        print(
            f"SKIP game {game_id}: "
            f"no player box score yet",
            flush=True,
        )
        return 0

    teams = team_maps(conn)
    rows = []

    for section in sections:
        team = section.get("team") or {}

        abbr = normalize_team(
            team.get("abbreviation")
        )

        team_id = teams.get(abbr)

        if team_id is None:
            raise RuntimeError(
                f"Unknown box score team "
                f"{abbr} for game {game_id}"
            )

        for group in (
            section.get("statistics") or []
        ):
            labels = (
                group.get("labels")
                or group.get("names")
                or []
            )

            for item in (
                group.get("athletes") or []
            ):
                athlete = (
                    item.get("athlete") or {}
                )

                athlete_id = athlete.get("id")

                full_name = (
                    athlete.get("displayName")
                    or athlete.get("fullName")
                )

                if not athlete_id or not full_name:
                    continue

                # Do not create fake zero
                # stats for players who did not play.
                if (
                    item.get("didNotPlay")
                    or item.get("reason")
                ):
                    continue

                values = item.get("stats") or []
                if game_id == "401909277":
    print(
        "DEBUG BOX SCORE:",
        "labels=", labels,
        "player=", full_name,
        "stats=", values,
        "didNotPlay=", item.get("didNotPlay"),
        "reason=", item.get("reason"),
        flush=True,
    )

                if not values or not labels:
                    continue

                stats = {
                    label_key(key): value
                    for key, value in zip(
                        labels, values
                    )
                }

                pts = number(
                    pick(
                        stats,
                        "PTS",
                        "POINTS",
                    )
                )

                reb = number(
                    pick(
                        stats,
                        "REB",
                        "REBOUNDS",
                    )
                )

                ast = number(
                    pick(
                        stats,
                        "AST",
                        "ASSISTS",
                    )
                )

                mins = minutes_value(
                    pick(
                        stats,
                        "MIN",
                        "MINUTES",
                    )
                )

                if (
                    pts is None
                    or reb is None
                    or ast is None
                    or mins is None
                ):
                    continue

                threes = pick(
                    stats,
                    "3PT",
                    "3PM-A",
                    "3PM",
                    "3P",
                )

                if (
                    threes is not None
                    and "-" in str(threes)
                ):
                    threes = str(
                        threes
                    ).split("-", 1)[0]

                # Negative IDs distinguish ESPN
                # player IDs from NBA player IDs.
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
                        number(threes),
                        number(
                            pick(
                                stats,
                                "STL",
                                "STEALS",
                            )
                        ),
                        number(
                            pick(
                                stats,
                                "BLK",
                                "BLOCKS",
                            )
                        ),
                        number(
                            pick(
                                stats,
                                "TO",
                                "TOV",
                                "TURNOVERS",
                            )
                        ),
                    )
                )

    if not rows:
        print(
            f"SKIP game {game_id}: "
            f"no complete player records",
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
                stl,
                blk,
                tov,
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
                VALUES (
                    %s, %s, %s, TRUE, NOW()
                )
                ON CONFLICT (id) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    updated_at = NOW()
                """,
                (
                    pid,
                    name,
                    tid,
                ),
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
                ON CONFLICT (
                    game_id, player_id
                )
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
                    stl,
                    blk,
                    tov,
                ),
            )

        team_counts = {}

        for row in rows:
            team_id = row[2]

            team_counts[team_id] = (
                team_counts.get(team_id, 0)
                + 1
            )

        # Basic completeness check.
        if (
            len(team_counts) == 2
            and min(team_counts.values()) >= 5
        ):
            cur.execute(
                """
                UPDATE games
                SET stats_verified = TRUE
                WHERE id = %s
                """,
                (game_id,),
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
            VALUES (
                %s, %s, NOW(), %s, %s, %s
            )
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
        raise RuntimeError(
            "DATABASE_URL is missing"
        )

    started = datetime.now(
        timezone.utc
    )

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

            # Last 7 days and next 3 days.
            for offset in range(-7, 4):
                day = today + timedelta(
                    days=offset
                )

                count, finals = sync_games(
                    conn,
                    day,
                )

                conn.commit()

                game_count += count
                final_ids.update(finals)

                print(
                    f"{day}: "
                    f"{count} games processed",
                    flush=True,
                )

            player_count = 0
            failed_games = []

            for game_id in sorted(final_ids):
                try:
                    count = sync_boxscore(
                        conn,
                        game_id,
                    )

                    conn.commit()

                    player_count += count

                    print(
                        f"{game_id}: "
                        f"{count} player rows",
                        flush=True,
                    )

                except Exception as exc:
                    conn.rollback()

                    failed_games.append(
                        f"{game_id}: {exc}"
                    )

                    print(
                        f"WARNING: boxscore "
                        f"{game_id}: {exc}",
                        flush=True,
                    )

            if failed_games:
                raise RuntimeError(
                    f"{len(failed_games)} "
                    f"box scores failed: "
                    + "; ".join(
                        failed_games[:3]
                    )
                )

            total = (
                team_count
                + game_count
                + player_count
            )

            record_sync(
                conn,
                started,
                "success",
                total,
            )

            conn.commit()

            print(
                f"SUCCESS: "
                f"{team_count} teams, "
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
