"""ZenStats: backfill NBA games, player bios, and full box scores."""

import json
import os
import time

from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg


BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
START_DATE = date(2026, 10, 20)
SEASON = "2026-27"
REQUEST_DELAY = 0.4


def full_box_fields(stats):
    """Missing source values remain unknown, not zero."""
    normalized = {
        str(key).strip().upper().replace(" ", "").replace("_", ""): value
        for key, value in stats.items()
    }

    def read(*keys):
        for key in keys:
            if key not in normalized:
                continue

            value = normalized[key]

            if (
                value is not None
                and str(value).strip().upper()
                not in ("", "--", "-", "N/A")
            ):
                return str(value).strip()

        return None

    def integer(value):
        if value is None:
            return None

        if not value.isdigit():
            raise ValueError("Invalid box-score count: " + value)

        return int(value)

    def pair(combined, made, attempted):
        value = read(*combined)

        if value is not None and "-" in value:
            parts = value.split("-", 1)
            result = tuple(integer(part.strip()) for part in parts)
        else:
            result = (
                integer(read(*made)),
                integer(read(*attempted)),
            )

        if (
            all(value is not None for value in result)
            and result[0] > result[1]
        ):
            raise ValueError("Made shots exceed attempted shots")

        return result

    fgm, fga = pair(
        ("FG", "FGM-A", "FGMA"),
        ("FGM", "FIELDGOALSMADE"),
        ("FGA", "FIELDGOALSATTEMPTED"),
    )

    ftm, fta = pair(
        ("FT", "FTM-A", "FTMA"),
        ("FTM", "FREETHROWSMADE"),
        ("FTA", "FREETHROWSATTEMPTED"),
    )

    fouls = integer(read("PF", "FOULS", "PERSONALFOULS"))
    offensive = integer(
        read("OREB", "OR", "OFF", "OFFENSIVEREBOUNDS")
    )
    defensive = integer(
        read("DREB", "DR", "DEF", "DEFENSIVEREBOUNDS")
    )
    total = integer(read("REB", "REBOUNDS"))

    if (
        all(value is not None for value in (
            offensive, defensive, total
        ))
        and offensive + defensive != total
    ):
        raise ValueError(
            "Offensive plus defensive rebounds differs from total"
        )

    return fgm, fga, ftm, fta, fouls, offensive, defensive


def fetch(path, params=None):
    url = BASE + path

    if params:
        url += "?" + urlencode(params)

    for attempt in range(4):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 ZenStats",
                    "Accept": "application/json",
                },
            )

            with urlopen(request, timeout=30) as response:
                data = json.load(response)

            time.sleep(REQUEST_DELAY)
            return data

        except Exception as error:
            print(
                f"Request failed ({attempt + 1}/4): "
                f"{url}: {error}",
                flush=True,
            )

            if attempt < 3:
                time.sleep(2 ** attempt)

    return None


def to_int(value):
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def to_minutes(value):
    try:
        value = str(value or "0")

        if ":" in value:
            minute, second = value.split(":", 1)
            return round(int(minute) + int(second) / 60, 3)

        return float(value)

    except (ValueError, TypeError):
        return 0.0


def get_teams(cursor):
    cursor.execute("SELECT id, abbreviation FROM teams")

    teams = {
        str(abbreviation).upper(): int(team_id)
        for team_id, abbreviation in cursor.fetchall()
        if abbreviation
    }

    aliases = {
        "GS": "GSW",
        "NY": "NYK",
        "SA": "SAS",
        "NO": "NOP",
        "UTAH": "UTA",
    }

    for alias, actual in aliases.items():
        if actual in teams:
            teams[alias] = teams[actual]

    if len(set(teams.values())) < 30:
        raise RuntimeError(
            "The database must contain all 30 NBA teams."
        )

    return teams


def save_player(cursor, athlete, team_id=None):
    raw_id = athlete.get("id")

    name = (
        athlete.get("displayName")
        or athlete.get("fullName")
        or athlete.get("name")
    )

    if not raw_id or not str(raw_id).isdigit() or not name:
        return None

    player_id = -int(raw_id)

    if team_id is None:
        cursor.execute(
            """
            INSERT INTO players (
                id, full_name, is_active, updated_at
            )
            VALUES (%s, %s, TRUE, NOW())
            ON CONFLICT (id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                updated_at = NOW()
            """,
            (player_id, name),
        )
    else:
        cursor.execute(
            """
            INSERT INTO players (
                id, full_name, current_team_id,
                listed_position, listed_height, listed_weight,
                is_active, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW())
            ON CONFLICT (id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                current_team_id = EXCLUDED.current_team_id,
                listed_position = COALESCE(
                    EXCLUDED.listed_position,
                    players.listed_position
                ),
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
            (
                player_id,
                name,
                team_id,
                (athlete.get("position") or {}).get("abbreviation")
                or None,
                athlete.get("displayHeight") or None,
                athlete.get("displayWeight") or None,
            ),
        )

    return player_id


def sync_rosters(cursor, teams):
    payload = fetch("/teams")

    if payload is None:
        print("Could not load team directory.", flush=True)
        return 0

    count = 0

    for sport in payload.get("sports") or []:
        for league in sport.get("leagues") or []:
            for entry in league.get("teams") or []:
                team = entry.get("team") or {}

                team_id = teams.get(
                    str(team.get("abbreviation", "")).upper()
                )
                espn_id = team.get("id")

                if team_id is None or not espn_id:
                    continue

                roster = fetch(f"/teams/{espn_id}/roster")

                if roster is None:
                    continue

                for group in roster.get("athletes") or []:
                    athletes = (
                        group.get("items") or []
                        if "items" in group
                        else [group]
                    )

                    for athlete in athletes:
                        player_id = save_player(
                            cursor, athlete, team_id
                        )

                        if player_id is not None:
                            count += 1

    return count


def save_game(cursor, event, day, teams):
    competitions = event.get("competitions") or []

    if not competitions:
        return None

    competitors = competitions[0].get("competitors") or []

    home = next(
        (
            item for item in competitors
            if item.get("homeAway") == "home"
        ),
        None,
    )
    away = next(
        (
            item for item in competitors
            if item.get("homeAway") == "away"
        ),
        None,
    )

    if home is None or away is None:
        return None

    home_abbr = str(
        (home.get("team") or {}).get("abbreviation", "")
    ).upper()
    away_abbr = str(
        (away.get("team") or {}).get("abbreviation", "")
    ).upper()

    home_id = teams.get(home_abbr)
    away_id = teams.get(away_abbr)

    if home_id is None or away_id is None:
        return None

    game_id = str(event.get("id") or "")

    if not game_id:
        return None

    status = (
        (event.get("status") or {}).get("type") or {}
    )

    completed = (
        status.get("completed") is True
        or status.get("state") == "post"
    )

    if not completed:
        return None

    cursor.execute(
        """
        INSERT INTO games (
            id, game_date, season,
            home_team_id, away_team_id,
            home_score, away_score,
            status, stats_verified, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, 'final', FALSE, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            game_date = EXCLUDED.game_date,
            season = EXCLUDED.season,
            home_team_id = EXCLUDED.home_team_id,
            away_team_id = EXCLUDED.away_team_id,
            home_score = EXCLUDED.home_score,
            away_score = EXCLUDED.away_score,
            status = 'final',
            updated_at = NOW()
        """,
        (
            game_id,
            day,
            SEASON,
            home_id,
            away_id,
            to_int(home.get("score")),
            to_int(away.get("score")),
        ),
    )

    return game_id


def save_boxscore(cursor, game_id, teams):
    payload = fetch("/summary", {"event": game_id})

    if payload is None:
        return None

    groups = (
        (payload.get("boxscore") or {}).get("players")
        or []
    )

    if len(groups) < 2:
        return None

    count = 0
    teams_with_stats = set()

    for group in groups:
        abbreviation = str(
            (group.get("team") or {}).get("abbreviation", "")
        ).upper()

        team_id = teams.get(abbreviation)

        if team_id is None:
            continue

        for category in group.get("statistics") or []:
            labels = [
                str(label).upper()
                for label in (
                    category.get("labels")
                    or category.get("names")
                    or []
                )
            ]

            for item in category.get("athletes") or []:
                if item.get("didNotPlay") is True:
                    continue

                raw_stats = item.get("stats") or []

                if not raw_stats:
                    continue

                stats = dict(zip(labels, raw_stats))

                played = (
                    to_minutes(stats.get("MIN")) > 0
                    or any(
                        to_int(stats.get(key)) > 0
                        for key in (
                            "PTS", "REB", "AST",
                            "STL", "BLK", "TO",
                        )
                    )
                )

                if not played:
                    continue

                athlete = item.get("athlete") or {}
                player_id = save_player(cursor, athlete)

                if player_id is None:
                    continue

                three_point_stat = (
                    stats.get("3PT")
                    or stats.get("3PM-A")
                    or stats.get("3PM")
                    or "0"
                )

                threes_made = to_int(
                    str(three_point_stat).split("-")[0]
                )

                attempt_parts = str(
                    three_point_stat
                ).split("-", 1)

                threes_attempted = None

                if len(attempt_parts) == 2:
                    try:
                        threes_attempted = int(attempt_parts[1])
                    except (ValueError, TypeError):
                        raise ValueError(
                            "Invalid three-point attempts"
                        )

                    if threes_attempted < threes_made:
                        raise ValueError(
                            "Three-point attempts below makes"
                        )

                cursor.execute(
                    """
                    INSERT INTO player_game_stats (
                        game_id, player_id, team_id,
                        minutes, points, rebounds,
                        assists, threes_made, threes_attempted,
                        steals, blocks, turnovers,
                        field_goals_made, field_goals_attempted,
                        free_throws_made, free_throws_attempted,
                        personal_fouls,
                        offensive_rebounds, defensive_rebounds
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (game_id, player_id)
                    DO UPDATE SET
                        team_id = EXCLUDED.team_id,
                        minutes = EXCLUDED.minutes,
                        points = EXCLUDED.points,
                        rebounds = EXCLUDED.rebounds,
                        assists = EXCLUDED.assists,
                        threes_made = EXCLUDED.threes_made,
                        threes_attempted = EXCLUDED.threes_attempted,
                        steals = EXCLUDED.steals,
                        blocks = EXCLUDED.blocks,
                        turnovers = EXCLUDED.turnovers,
                        field_goals_made = COALESCE(
                            EXCLUDED.field_goals_made,
                            player_game_stats.field_goals_made
                        ),
                        field_goals_attempted = COALESCE(
                            EXCLUDED.field_goals_attempted,
                            player_game_stats.field_goals_attempted
                        ),
                        free_throws_made = COALESCE(
                            EXCLUDED.free_throws_made,
                            player_game_stats.free_throws_made
                        ),
                        free_throws_attempted = COALESCE(
                            EXCLUDED.free_throws_attempted,
                            player_game_stats.free_throws_attempted
                        ),
                        personal_fouls = COALESCE(
                            EXCLUDED.personal_fouls,
                            player_game_stats.personal_fouls
                        ),
                        offensive_rebounds = COALESCE(
                            EXCLUDED.offensive_rebounds,
                            player_game_stats.offensive_rebounds
                        ),
                        defensive_rebounds = COALESCE(
                            EXCLUDED.defensive_rebounds,
                            player_game_stats.defensive_rebounds
                        )
                    """,
                    (
                        game_id,
                        player_id,
                        team_id,
                        to_minutes(stats.get("MIN")),
                        to_int(stats.get("PTS")),
                        to_int(stats.get("REB")),
                        to_int(stats.get("AST")),
                        threes_made,
                        threes_attempted,
                        to_int(stats.get("STL")),
                        to_int(stats.get("BLK")),
                        to_int(stats.get("TO")),
                        *full_box_fields(stats),
                    ),
                )

                count += 1
                teams_with_stats.add(team_id)

    if count >= 10 and len(teams_with_stats) == 2:
        cursor.execute(
            """
            UPDATE games
            SET stats_verified = TRUE,
                updated_at = NOW()
            WHERE id = %s
            """,
            (game_id,),
        )

        return count

    return None


def main():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL secret is missing.")

    today = datetime.now(timezone.utc).date()

    print(
        "ZENSTATS PLAYER BIOS AND FULL BOX-SCORE BACKFILL",
        f"Season: {SEASON}",
        f"Dates: {START_DATE} through {today}",
        sep="\n",
        flush=True,
    )

    failed = []
    games_updated = 0
    rows_updated = 0

    with psycopg.connect(
        database_url,
        connect_timeout=15,
        sslmode="require",
    ) as connection:
        with connection.cursor() as cursor:
            teams = get_teams(cursor)

            roster_count = sync_rosters(cursor, teams)
            connection.commit()

            print(
                f"Roster entries updated: {roster_count}",
                flush=True,
            )

            day = START_DATE

            while day <= today:
                scoreboard = fetch(
                    "/scoreboard",
                    {
                        "dates": day.strftime("%Y%m%d"),
                        "limit": 100,
                        "seasontype": 2,
                    },
                )

                if scoreboard is None:
                    failed.append(
                        f"Scoreboard unavailable: {day}"
                    )
                    day += timedelta(days=1)
                    continue

                events = scoreboard.get("events") or []

                for event in events:
                    game_id = None

                    try:
                        game_id = save_game(
                            cursor, event, day, teams
                        )

                        if game_id is None:
                            connection.commit()
                            continue

                        count = save_boxscore(
                            cursor, game_id, teams
                        )

                        if count is None:
                            failed.append(
                                f"Incomplete boxscore: {game_id}"
                            )
                        else:
                            games_updated += 1
                            rows_updated += count

                        connection.commit()

                    except Exception as error:
                        connection.rollback()
                        failed.append(
                            f"Game {game_id}: {error}"
                        )

                print(
                    f"{day}: {len(events)} events checked",
                    flush=True,
                )

                day += timedelta(days=1)

            cursor.execute("SELECT COUNT(*) FROM players")
            player_count = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM player_game_stats"
            )
            stat_count = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM games
                WHERE status = 'final'
                  AND game_date BETWEEN %s AND %s
                  AND stats_verified = FALSE
                """,
                (START_DATE, today),
            )
            unverified_count = cursor.fetchone()[0]

            print(
                "\nDATABASE SUMMARY",
                f"Players: {player_count}",
                f"Player-game records: {stat_count}",
                f"Boxscores updated: {games_updated}",
                f"Rows processed: {rows_updated}",
                f"Unverified final games: {unverified_count}",
                sep="\n",
                flush=True,
            )

    if failed:
        print(
            "\nINCOMPLETE REQUESTS:",
            *failed,
            sep="\n- ",
            flush=True,
        )

        raise RuntimeError(
            f"{len(failed)} games or requests "
            "need another backfill attempt."
        )

    print("SEASON BACKFILL FINISHED", flush=True)


if __name__ == "__main__":
    main()
