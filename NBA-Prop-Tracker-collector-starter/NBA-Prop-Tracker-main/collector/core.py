"""NBA CDN final box-score collector. Unofficial endpoint; availability/terms may change."""
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

BASE = 'https://cdn.nba.com/static/json/liveData'
LOG = logging.getLogger(__name__)


def fetch_json(url, attempts=3):
    for attempt in range(attempts):
        try:
            req = Request(url, headers={'User-Agent': 'NBAPropTracker/0.1 (personal stats collector)', 'Accept': 'application/json'})
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def scoreboard_games(fetch=fetch_json):
    data = fetch(f'{BASE}/scoreboard/todaysScoreboard_00.json')
    return data['scoreboard']['games']


def final_game_ids(games):
    return [str(g['gameId']) for g in games if int(g.get('gameStatus', 0)) == 3]


def boxscore(game_id, fetch=fetch_json):
    return fetch(f'{BASE}/boxscore/boxscore_{game_id}.json')['game']


def init_db(conn):
    conn.executescript('''
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS games (
      game_id TEXT PRIMARY KEY, game_date TEXT NOT NULL, home_team TEXT NOT NULL,
      away_team TEXT NOT NULL, home_score INTEGER NOT NULL, away_score INTEGER NOT NULL,
      published_at TEXT NOT NULL, checked_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS player_games (
      game_id TEXT NOT NULL REFERENCES games(game_id), player_id INTEGER NOT NULL,
      player_name TEXT NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
      venue TEXT NOT NULL CHECK(venue IN ('Home','Away')),
      minutes REAL NOT NULL, points INTEGER NOT NULL, rebounds INTEGER NOT NULL,
      assists INTEGER NOT NULL, threes INTEGER NOT NULL, steals INTEGER NOT NULL,
      blocks INTEGER NOT NULL, turnovers INTEGER NOT NULL, fgm INTEGER NOT NULL,
      fga INTEGER NOT NULL, ftm INTEGER NOT NULL, fta INTEGER NOT NULL,
      PRIMARY KEY (game_id, player_id)
    );
    CREATE INDEX IF NOT EXISTS idx_player_games ON player_games(player_id, game_id);
    ''')
    conn.commit()


def parse_minutes(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    if ':' in str(value):
        mm, ss = str(value).split(':', 1)
        return int(mm) + int(ss) / 60
    return float(value)


def parse_game(game):
    if int(game.get('gameStatus', 0)) != 3:
        raise ValueError('Box score is not final')
    home, away = game['homeTeam'], game['awayTeam']
    home_abbr, away_abbr = home['teamTricode'], away['teamTricode']
    game_id = str(game['gameId'])
    date = game.get('gameTimeUTC') or game.get('gameEt')
    if not date:
        raise ValueError('Missing game date')
    rows = []
    for team, opponent, venue in ((home, away_abbr, 'Home'), (away, home_abbr, 'Away')):
        for p in team.get('players', []):
            # Exclude DNP/inactive players from completed-game logs.
            if p.get('played') is False or p.get('notPlayingReason'):
                continue
            s = p.get('statistics')
            if not s or not s.get('minutes'):
                continue
            def num(key):
                v = s.get(key)
                if v is None:
                    raise ValueError(f'Missing {key} for player {p.get("personId")}')
                return int(v)
            mins = parse_minutes(s['minutes'])
            if not 0 <= mins <= 70:
                raise ValueError('Invalid player minutes')
            rows.append((game_id, int(p['personId']), f"{p.get('firstName','')} {p.get('familyName','')}".strip(),
                         team['teamTricode'], opponent, venue, mins,
                         num('points'), num('reboundsTotal'), num('assists'),
                         num('threePointersMade'), num('steals'), num('blocks'),
                         num('turnovers'), num('fieldGoalsMade'), num('fieldGoalsAttempted'),
                         num('freeThrowsMade'), num('freeThrowsAttempted')))
    if len(rows) < 10 or len({r[1] for r in rows}) != len(rows):
        raise ValueError('Incomplete or duplicate player box score')
    for row in rows:
        _, _, _, _, _, _, _, pts, reb, ast, threes, steals, blocks, to, fgm, fga, ftm, fta = row
        if any(v < 0 for v in (pts,reb,ast,threes,steals,blocks,to,fgm,fga,ftm,fta)):
            raise ValueError('Negative stat')
        if fgm > fga or ftm > fta or threes > fgm or pts < 2 * fgm + ftm + threes:
            raise ValueError('Inconsistent shooting totals')
    scores = (int(home['score']), int(away['score']))
    for abbr, score in ((home_abbr, scores[0]), (away_abbr, scores[1])):
        total = sum(r[7] for r in rows if r[3] == abbr)
        if total != score:
            raise ValueError(f'{abbr} player points {total} != team score {score}')
    return (game_id, date, home_abbr, away_abbr, *scores), rows


def save_final(conn, game):
    game_row, player_rows = parse_game(game)
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute('''INSERT INTO games VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(game_id) DO UPDATE SET game_date=excluded.game_date,
          home_team=excluded.home_team,away_team=excluded.away_team,
          home_score=excluded.home_score,away_score=excluded.away_score,checked_at=excluded.checked_at''',
          (*game_row, now, now))
        conn.execute('DELETE FROM player_games WHERE game_id=?', (game_row[0],))
        conn.executemany('INSERT INTO player_games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', player_rows)
    return len(player_rows)


def sync(conn, fetch=fetch_json, force=False):
    games = scoreboard_games(fetch)
    ids = final_game_ids(games)
    results = {}
    for game_id in ids:
        existing = conn.execute('SELECT checked_at FROM games WHERE game_id=?', (game_id,)).fetchone()
        if existing and not force:
            checked = datetime.fromisoformat(existing[0])
            if (datetime.now(timezone.utc) - checked).total_seconds() < 3600:
                continue
        try:
            count = save_final(conn, boxscore(game_id, fetch))
            results[game_id] = f'published {count} players'
        except Exception as exc:
            LOG.exception('Could not publish game %s: %s', game_id, exc)
            results[game_id] = f'not published: {exc}'
    return results


def recent_games(conn, player_id, n=5, venue='All'):
    if n not in (5, 10, 20) or venue not in ('All', 'Home', 'Away'):
        raise ValueError('Invalid selection')
    sql = '''SELECT g.game_date, pg.opponent, pg.venue, pg.minutes, pg.points,
      pg.rebounds, pg.assists, pg.threes, pg.steals, pg.blocks
      FROM player_games pg JOIN games g USING(game_id) WHERE pg.player_id=?'''
    args = [player_id]
    if venue != 'All':
        sql += ' AND pg.venue=?'
        args.append(venue)
    sql += ' ORDER BY g.game_date DESC, g.game_id DESC LIMIT ?'
    args.append(n)
    return conn.execute(sql, args).fetchall()
