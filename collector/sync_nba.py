
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from nba_api.stats.static import teams as nba_teams

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
VERSION = "2026-11-06-season-backfill-v1"

SEASON_START = date(2026, 10, 20)
BACKFILL_DAYS_PER_RUN = 5
RECENT_DAYS = 7
FUTURE_DAYS = 3

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

    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                request, timeout=30
            ) as response:
                return json.load(response)
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def normalized_abbr(value):
    key = str(value or "").strip().upper()
    return TEAM_ALIASES.get(key, key)


def get_team_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, abbreviation FROM teams")
        return {
            normalized_abbr(abbr): team_id
            for team_id, abbr in cur.fetchall()
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


def safe_int(value):
    if value is None:
        return None

    if str(value).strip().upper() in (
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

    try:
        value = str(value).strip()

        if ":" in value:
            mins, secs = value.split(":", 1)
            return round(
                int(mins) + int(secs) / 60, 3
            )

        return float(value)

    except (ValueError, TypeError):
        return None


def shots_made(value):
    if value is None:
        return None

    return safe_int(
        str(value).split("-", 1)[0]
    )


def pick(stats, *names):
    for name in names:
        if name in stats:
            return stats[name]
    return None


def sync_games_for_date(conn, day, team_ids):
    data = fetch_json(
        f"{BASE}/scoreboard?dates={day:%Y%m%d}&limit=100"
    )

    count = 0
    finals = []

    with conn.cursor() as cur:
        for event in data.get("events", []):
            competition = (
                event.get("competitions") or [{}]
            )[0]

            competitors = {
                entry.get("homeAway"): entry
                for entry in competition.get(
                    "competitors", []
                )
            }

            home = competitors.get("home")
            away = competitors.get("away")

            if (
                not home
                or not away
                or not event.get("date")
            ):
                continue

            home_abbr = normalized_abbr(
                (home.get("team") or {}).get(
                    "abbreviation"
                )
            )

            away_abbr = normalized_abbr(
                (away.get("team") or {}).get(
                    "abbreviation"
                )
            )

            home_id = team_ids.get(home_abbr)
            away_id = team_ids.get(away_abbr)

            if home_id is None or away_id is None:
                print(
                    f"SKIP unknown teams: "
                    f"{away_abbr} @ {home_abbr}",
                    flush=True,
                )
                continue

            status_type = (
                (event.get("status") or {}).get(
                    "type"
                ) or {}
            )

            complete = bool(
                status_type.get("completed")
            )

            if complete:
                status = "final"
            elif status_type.get("state") == "in":
                status = "live"
            else:
                status = "scheduled"

            game_date = datetime.fromisoformat(
                event["date"].replace(
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
                    id, game_date, season,
                    home_team_id, away_team_id,
                    home_score, away_score,
                    status, stats_verified, updated_at
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
                    game_date,
                    season_value,
                    home_id,
                    away_id,
                    safe_int(home.get("score")),
                    safe_int(away.get("score")),
                    status,
                ),
            )

            count += 1

            if complete:
                finals.append(game_id)

    return count, finals


def sync_boxscore(conn, game_id, team_ids):
    summary = fetch_json(
        f"{BASE}/summary?event={game_id}"
    )

    sections = (
        (summary.get("boxscore") or {}).get(
            "players"
        ) or []
    )

    if not sections:
        print(
            f"SKIP {game_id}: box score unavailable",
            flush=True,
        )
        return 0

    rows = []
    skipped = {}

    for section in sections:
        team = section.get("team") or {}

        abbr = normalized_abbr(
            team.get("abbreviation")
        )

        team_id = team_ids.get(abbr)

        if team_id is None:
            raise RuntimeError(
                f"Unknown box score team {abbr} "
                f"for game {game_id}"
            )

        for group in section.get(
            "statistics"
        ) or []:
            labels = (
                group.get("labels")
                or group.get("names")
                or []
            )

            for item in group.get(
                "athletes"
            ) or []:
                athlete = item.get(
                    "athlete"
                ) or {}

                athlete_id = athlete.get("id")

                name = (
                    athlete.get("displayName")
                    or athlete.get("fullName")
                )

                values = item.get(
                    "stats"
                ) or []

                if not athlete_id or not name:
                    skipped["identity"] = (
                        skipped.get("identity", 0) + 1
                    )
                    continue

                # Only skip actual DNP players.
                # ESPN may include a reason even
                # when a player participated.
                if item.get("didNotPlay") is True:
                    skipped["actual DNP"] = (
                        skipped.get("actual DNP", 0)
                        + 1
                    )
                    continue

                if not labels or not values:
                    skipped["missing stats"] = (
                        skipped.get(
                            "missing stats", 0
                        ) + 1
                    )
                    continue

                stats = {
                    str(label)
                    .strip()
                    .upper()
                    .replace(" ", "")
                    .replace("_", ""): value
                    for label, value in zip(
                        labels, values
                    )
                }

                points = safe_int(
                    pick(
                        stats,
                        "PTS",
                        "POINTS",
                    )
                )

                rebounds = safe_int(
                    pick(
                        stats,
                        "REB",
                        "REBOUNDS",
                    )
                )

                assists = safe_int(
                    pick(
                        stats,
                        "AST",
                        "ASSISTS",
                    )
                )

                minutes = minutes_value(
                    pick(
                        stats,
                        "MIN",
                        "MINUTES",
                    )
                )

                if any(
                    x is None
                    for x in (
                        points,
                        rebounds,
                        assists,
                        minutes,
                    )
                ):
                    skipped["incomplete stats"] = (
                        skipped.get(
                            "incomplete stats", 0
                        ) + 1
                    )
                    continue

                threes = shots_made(
                    pick(
                        stats,
                        "3PT",
                        "3PM-A",
                        "3PM",
                        "3P",
                    )
                )

                steals = safe_int(
                    pick(
                        stats,
                        "STL",
                        "STEALS",
                    )
                )

                blocks = safe_int(
                    pick(
                        stats,
                        "BLK",
                        "BLOCKS",
                    )
                )

                turnovers = safe_int(
                    pick(
                        stats,
                        "TO",
                        "TOV",
                        "TURNOVERS",
                    )
                )

                rows.append(
                    (
                        game_id,
                        -int(athlete_id),
                        team_id,
                        name,
                        minutes,
                        points,
                        rebounds,
                        assists,
                        threes,
                        steals,
                        blocks,
                        turnovers,
                    )
                )

    if skipped:
        print(
            f"BOX SCORE {game_id} skipped: "
            f"{skipped}",
            flush=True,
        )

    if not rows:
        print(
            f"SKIP {game_id}: "
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
                steals,
                blocks,
                turnovers,
            ) = row

            cur.execute(
                """
                INSERT INTO players (
                    id, full_name, current_team_id,
                    is_active, updated_at
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
                    game_id, player_id, team_id,
                    minutes, points, rebounds,
                    assists, threes_made,
                    steals, blocks, turnovers
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
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

    print(
        f"BOX SCORE {game_id}: "
        f"saved {len(rows)} players",
        flush=True,
    )

    return len(rows)


def select_backfill_dates(conn, today):
    last_old_day = (
        today
        - timedelta(days=RECENT_DAYS + 1)
    )

    if last_old_day < SEASON_START:
        return []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT g.game_date
            FROM games g
            WHERE g.status = 'final'
              AND g.game_date >= %s
              AND g.game_date <= %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM player_game_stats p
                  WHERE p.game_id = g.id
              )
            """,
            (
                SEASON_START,
                last_old_day,
            ),
        )

        missing_known = {
            row[0]
            for row in cur.fetchall()
        }

        cur.execute(
            """
            SELECT DISTINCT game_date
            FROM games
            WHERE game_date >= %s
              AND game_date <= %s
            """,
            (
                SEASON_START,
                last_old_day,
            ),
        )

        known_dates = {
            row[0]
            for row in cur.fetchall()
        }

    selected = []
    day = SEASON_START

    while (
        day <= last_old_day
        and len(selected) < BACKFILL_DAYS_PER_RUN
    ):
        if (
            day not in known_dates
            or day in missing_known
        ):
            selected.append(day)

        day += timedelta(days=1)

    return selected


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

    print(
        f"COLLECTOR VERSION: {VERSION}",
        flush=True,
    )

    started = datetime.now(timezone.utc)

    with psycopg.connect(
        url,
        connect_timeout=10,
        sslmode="require",
    ) as conn:
        try:
            team_count = sync_teams(conn)
            conn.commit()

            team_ids = get_team_ids(conn)

            today = datetime.now(
                ZoneInfo("America/New_York")
            ).date()

            backfill_dates = select_backfill_dates(
                conn, today
            )

            print(
                f"HISTORICAL BACKFILL: "
                f"{len(backfill_dates)} days: "
                f"{backfill_dates}",
                flush=True,
            )

            dates = sorted(
                set(backfill_dates)
                | {
                    today + timedelta(days=offset)
                    for offset in range(
                        -RECENT_DAYS,
                        FUTURE_DAYS + 1,
                    )
                }
            )

            game_count = 0
            final_ids = set()

            for day in dates:
                count, finals = sync_games_for_date(
                    conn,
                    day,
                    team_ids,
                )

                conn.commit()

                game_count += count
                final_ids.update(finals)

                print(
                    f"{day}: {count} games processed",
                    flush=True,
                )

            old_cutoff = (
                today
                - timedelta(days=RECENT_DAYS + 1)
            )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT game_id
                    FROM player_game_stats
                    WHERE game_id = ANY(%s)
                    """,
                    (list(final_ids),),
                )

                already_saved = {
                    str(row[0])
                    for row in cur.fetchall()
                }

                cur.execute(
                    """
                    SELECT id, game_date
                    FROM games
                    WHERE id = ANY(%s)
                    """,
                    (list(final_ids),),
                )

                game_dates = {
                    str(gid): day
                    for gid, day in cur.fetchall()
                }

            player_rows = 0
            failures = []

            for game_id in sorted(final_ids):
                game_day = game_dates.get(
                    game_id, today
                )

                if (
                    game_day <= old_cutoff
                    and game_id in already_saved
                ):
                    continue

                try:
                    count = sync_boxscore(
                        conn,
                        game_id,
                        team_ids,
                    )

                    conn.commit()
                    player_rows += count

                    print(
                        f"{game_id}: "
                        f"{count} player rows",
                        flush=True,
                    )

                except Exception as exc:
                    conn.rollback()

                    failures.append(
                        f"{game_id}: {exc}"
                    )

                    print(
                        f"WARNING: {game_id}: {exc}",
                        flush=True,
                    )

            if failures:
                raise RuntimeError(
                    f"{len(failures)} "
                    f"box scores failed: "
                    + "; ".join(failures[:3])
                )

            record_sync(
                conn,
                started,
                "success",
                team_count
                + game_count
                + player_rows,
            )

            conn.commit()

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM players"
                )
                saved_players = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM player_game_stats
                    """
                )
                saved_stats = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT COUNT(DISTINCT game_id)
                    FROM player_game_stats
                    """
                )
                games_with_stats = (
                    cur.fetchone()[0]
                )

                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM games g
                    WHERE g.status = 'final'
                      AND g.game_date >= %s
                      AND g.game_date <= %s
                      AND NOT EXISTS (
                          SELECT 1
                          FROM player_game_stats p
                          WHERE p.game_id = g.id
                      )
                    """,
                    (
                        SEASON_START,
                        today,
                    ),
                )

                known_missing_games = (
                    cur.fetchone()[0]
                )

            print(
                f"DATABASE CHECK: "
                f"{saved_players} players, "
                f"{saved_stats} player-game stats, "
                f"{games_with_stats} "
                f"games with stats",
                flush=True,
            )

            print(
                f"BACKFILL CHECK: "
                f"{known_missing_games} known "
                f"final games without player stats",
                flush=True,
            )

            print(
                f"SUCCESS: {team_count} teams, "
                f"{game_count} games processed, "
                f"{player_rows} "
                f"player rows processed",
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
