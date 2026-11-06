
import json
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs

import psycopg

HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NBA Prop Tracker</title>
<style>
:root {
  color-scheme: dark;
  font-family: Inter, system-ui, Arial, sans-serif;
  background: #0c0b14;
  color: #f4f1ff;
}
* { box-sizing: border-box; }
body { margin: 0; }
header {
  padding: 24px max(20px, calc((100vw - 1100px)/2));
  border-bottom: 1px solid #2d2540;
  background: #151021;
}
header h1 { margin: 0; font-size: 27px; }
header p { color: #a9a0bf; margin: 7px 0 0; }
main { max-width: 1100px; margin: 26px auto; padding: 0 20px; }
.panel {
  background: #181324;
  border: 1px solid #352a4b;
  border-radius: 15px;
  padding: 20px;
  margin-bottom: 20px;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: 1;
  min-width: 130px;
}
label { font-size: 12px; color: #c8b9e1; }
input, select, button {
  font: inherit;
  border-radius: 9px;
  padding: 11px;
  background: #100d19;
  color: #fff;
  border: 1px solid #55416d;
}
button {
  background: #7440d8;
  border: 0;
  cursor: pointer;
  font-weight: 700;
}
button:hover { background: #8b5af2; }
.results {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.player {
  background: #29203c;
  border: 1px solid #503a70;
  font-size: 13px;
}
.muted { color: #a9a0bf; }
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin: 18px 0;
}
.metric {
  background: #231b33;
  border: 1px solid #382a50;
  padding: 16px;
  border-radius: 12px;
}
.metric strong {
  font-size: 26px;
  display: block;
  margin-top: 5px;
}
.green { color: #6be0a1; }
.red { color: #ff8b97; }
table {
  border-collapse: collapse;
  width: 100%;
  min-width: 680px;
}
th, td {
  text-align: left;
  padding: 11px;
  border-bottom: 1px solid #362a49;
  font-size: 13px;
}
th { color: #c6b6e0; }
.scroll { overflow-x: auto; }
h2 { font-size: 19px; margin: 0 0 15px; }
.note { font-size: 12px; color: #a9a0bf; }
#status { margin: 12px 0; color: #c6b6e0; }
</style>
</head>
<body>
<header>
  <h1>🏀 NBA Prop Tracker</h1>
  <p>Player stats • Game trends • Custom prop lines</p>
</header>
<main>
<section class="panel">
  <h2>Find a player</h2>
  <div class="controls">
    <div class="field">
      <label for="search">Search NBA players</label>
      <input id="search" placeholder="e.g. Stephen Curry"
             autocomplete="off">
    </div>
    <button id="searchBtn">Search</button>
  </div>
  <div id="results" class="results"></div>
  <p id="status">Search for a player to begin.</p>
</section>

<section id="analysis" hidden>
  <div class="panel">
    <h2 id="playerTitle">Player</h2>
    <div class="controls">
      <div class="field">
        <label for="stat">Stat category</label>
        <select id="stat">
          <option value="points">Points</option>
          <option value="rebounds">Rebounds</option>
          <option value="assists">Assists</option>
          <option value="threes_made">3-Pointers Made</option>
          <option value="steals">Steals</option>
          <option value="blocks">Blocks</option>
          <option value="pr">Points + Rebounds</option>
          <option value="pa">Points + Assists</option>
          <option value="ra">Rebounds + Assists</option>
          <option value="pra">Points + Rebounds + Assists</option>
        </select>
      </div>
      <div class="field">
        <label for="window">Games</label>
        <select id="window">
          <option value="5">Last 5</option>
          <option value="10">Last 10</option>
          <option value="20">Last 20</option>
          <option value="all">Season (all available)</option>
        </select>
      </div>
      <div class="field">
        <label for="venue">Location</label>
        <select id="venue">
          <option value="all">All games</option>
          <option value="home">Home</option>
          <option value="away">Away</option>
        </select>
      </div>
      <div class="field">
        <label for="line">FanDuel line</label>
        <input id="line" type="number" step="0.5"
               min="0" value="24.5">
      </div>
    </div>
    <p class="note">
      Location filter is applied before selecting the last N games.
      Only final games with saved stats count.
    </p>
    <div id="metrics" class="metrics"></div>
    <p id="sample" class="note"></p>
  </div>
  <div class="panel">
    <h2>Game-by-game results</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Matchup</th>
            <th>Location</th>
            <th>MIN</th>
            <th>Stat</th>
            <th>Line result</th>
          </tr>
        </thead>
        <tbody id="games"></tbody>
      </table>
    </div>
  </div>
</section>
<p class="note">
  Statistics come from saved ESPN game box scores.
  Hit rates are historical, not predictions.
  FanDuel lines are entered manually.
</p>
</main>
<script>
const $ = id => document.getElementById(id);
let selected = null;
let history = [];
const fields = ['stat', 'window', 'venue', 'line'];

async function getJSON(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) {
    throw Error(data.error || 'Request failed');
  }
  return data;
}

function showStatus(message) {
  $('status').textContent = message;
}

async function search() {
  const query = $('search').value.trim();
  $('results').replaceChildren();

  if (!query) {
    showStatus('Enter a player name.');
    return;
  }

  showStatus('Searching...');

  try {
    const data = await getJSON(
      '/api/players?q=' + encodeURIComponent(query)
    );

    if (!data.players.length) {
      showStatus('No players found.');
      return;
    }

    showStatus(data.players.length + ' player(s) found.');

    for (const player of data.players) {
      const button = document.createElement('button');
      button.className = 'player';
      button.textContent = player.name;
      button.onclick = () => loadPlayer(player);
      $('results').append(button);
    }
  } catch (error) {
    showStatus(error.message);
  }
}

async function loadPlayer(player) {
  selected = player;
  showStatus('Loading ' + player.name + '...');

  try {
    const data = await getJSON(
      '/api/players/' +
      encodeURIComponent(player.id) +
      '/games'
    );

    history = data.games;
    $('playerTitle').textContent = player.name;
    $('analysis').hidden = false;

    showStatus(
      'Loaded ' + history.length +
      ' saved games for ' + player.name + '.'
    );

    render();
  } catch (error) {
    showStatus(error.message);
  }
}

function stat(game, key) {
  const value = field => Number(game[field] || 0);

  switch (key) {
    case 'pr':
      return value('points') + value('rebounds');
    case 'pa':
      return value('points') + value('assists');
    case 'ra':
      return value('rebounds') + value('assists');
    case 'pra':
      return value('points') +
             value('rebounds') +
             value('assists');
    default:
      return value(key);
  }
}

function cell(row, value, className) {
  const td = document.createElement('td');
  td.textContent = String(value);
  if (className) td.className = className;
  row.append(td);
}

function render() {
  if (!selected) return;

  const key = $('stat').value;
  const venue = $('venue').value;
  const windowValue = $('window').value;
  const line = Number($('line').value);

  const validLine =
    $('line').value.trim() !== '' &&
    Number.isFinite(line) &&
    line >= 0;

  const filtered = history.filter(
    game => venue === 'all' || game.venue === venue
  );

  const chosen = windowValue === 'all'
    ? filtered
    : filtered.slice(0, Number(windowValue));

  const values = chosen.map(game => stat(game, key));
  const total = values.reduce((a, b) => a + b, 0);

  const overs = validLine
    ? values.filter(value => value > line).length
    : 0;

  const unders = validLine
    ? values.filter(value => value < line).length
    : 0;

  const pushes = validLine
    ? values.filter(value => value === line).length
    : 0;

  const average = chosen.length
    ? (total / chosen.length).toFixed(1)
    : '—';

  const percentage = chosen.length
    ? (100 * overs / chosen.length).toFixed(1) + '%'
    : '—';

  const items = [
    ['Games', chosen.length],
    ['Average', average],
    ['Over hit rate', validLine ? percentage : 'Enter line'],
    [
      'Overs / Unders / Pushes',
      validLine
        ? overs + ' / ' + unders + ' / ' + pushes
        : '—'
    ]
  ];

  $('metrics').replaceChildren();

  for (const [name, value] of items) {
    const box = document.createElement('div');
    box.className = 'metric';

    const label = document.createElement('span');
    label.className = 'muted';
    label.textContent = name;

    const number = document.createElement('strong');
    number.textContent = value;

    box.append(label, number);
    $('metrics').append(box);
  }

  $('sample').textContent =
    'Showing ' + chosen.length +
    ' of ' + filtered.length +
    ' matching saved games. Season view uses available 2026–27 data.';

  $('games').replaceChildren();

  for (const game of chosen) {
    const row = document.createElement('tr');
    const value = stat(game, key);

    cell(row, game.date);
    cell(row, game.matchup);
    cell(row, game.venue === 'home' ? 'Home' : 'Away');
    cell(row, game.minutes === null ? '—' : game.minutes);
    cell(row, value);

    const result = !validLine
      ? '—'
      : value > line
        ? 'OVER'
        : value < line
          ? 'UNDER'
          : 'PUSH';

    cell(
      row,
      result,
      result === 'OVER'
        ? 'green'
        : result === 'UNDER'
          ? 'red'
          : ''
    );

    $('games').append(row);
  }
}

$('searchBtn').onclick = search;

$('search').addEventListener('keydown', event => {
  if (event.key === 'Enter') search();
});

for (const field of fields) {
  $(field).addEventListener('change', render);
}

$('line').addEventListener('input', render);
</script>
</body>
</html>'''


def respond(start_response, data, status="200 OK"):
    body = json.dumps(data, default=str).encode("utf-8")

    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])

    return [body]


def database():
    url = os.environ.get("DATABASE_URL")

    if not url:
        raise RuntimeError("Database is not configured")

    return psycopg.connect(
        url,
        connect_timeout=5,
        sslmode="require",
    )


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")

    if method != "GET":
        return respond(
            start_response,
            {"error": "Method not allowed"},
            "405 Method Not Allowed",
        )

    if path == "/":
        body = HTML.encode("utf-8")

        start_response("200 OK", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ])

        return [body]

    if path == "/health":
        try:
            with database() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM teams")
                count = cur.fetchone()[0]

            return respond(start_response, {
                "service": "NBA Prop Tracker",
                "status": "online",
                "database": "connected",
                "teams_in_database": count,
                "checked_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            })

        except Exception:
            return respond(
                start_response,
                {
                    "service": "NBA Prop Tracker",
                    "status": "online",
                    "database": "connection_failed",
                },
                "503 Service Unavailable",
            )

    if path == "/api/players":
        query = parse_qs(
            environ.get("QUERY_STRING", "")
        ).get("q", [""])[0].strip()

        if not query:
            return respond(
                start_response,
                {"players": []},
            )

        if len(query) > 80:
            return respond(
                start_response,
                {"error": "Search too long"},
                "400 Bad Request",
            )

        try:
            with database() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id, p.full_name
                    FROM players p
                    WHERE p.full_name ILIKE %s
                    ORDER BY
                        CASE
                            WHEN lower(p.full_name) = lower(%s)
                            THEN 0
                            ELSE 1
                        END,
                        p.full_name
                    LIMIT 30
                    """,
                    ("%" + query + "%", query),
                )

                players = [
                    {"id": pid, "name": name}
                    for pid, name in cur.fetchall()
                ]

            return respond(
                start_response,
                {"players": players},
            )

        except Exception:
            return respond(
                start_response,
                {"error": "Database query failed"},
                "503 Service Unavailable",
            )

    if (
        path.startswith("/api/players/")
        and path.endswith("/games")
    ):
        value = path[
            len("/api/players/"):-len("/games")
        ].strip("/")

        try:
            player_id = int(value)
        except ValueError:
            return respond(
                start_response,
                {"error": "Invalid player ID"},
                "400 Bad Request",
            )

        try:
            with database() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        g.game_date,
                        s.minutes,
                        s.points,
                        s.rebounds,
                        s.assists,
                        s.threes_made,
                        s.steals,
                        s.blocks,
                        s.team_id,
                        g.home_team_id,
                        g.away_team_id,
                        ht.abbreviation,
                        at.abbreviation
                    FROM player_game_stats s
                    JOIN games g
                        ON g.id = s.game_id
                    JOIN teams ht
                        ON ht.id = g.home_team_id
                    JOIN teams at
                        ON at.id = g.away_team_id
                    WHERE s.player_id = %s
                      AND g.status = 'final'
                      AND g.game_date >= DATE '2026-10-20'
                      AND g.game_date < DATE '2027-07-01'
                    ORDER BY
                        g.game_date DESC,
                        g.id DESC
                    LIMIT 120
                    """,
                    (player_id,),
                )

                rows = cur.fetchall()

            games = []

            for (
                game_date,
                minutes,
                points,
                rebounds,
                assists,
                threes,
                steals,
                blocks,
                team_id,
                home_id,
                away_id,
                home_abbr,
                away_abbr,
            ) in rows:
                games.append({
                    "date": game_date.isoformat(),
                    "minutes": (
                        float(minutes)
                        if minutes is not None
                        else None
                    ),
                    "points": points,
                    "rebounds": rebounds,
                    "assists": assists,
                    "threes_made": threes,
                    "steals": steals,
                    "blocks": blocks,
                    "venue": (
                        "home"
                        if team_id == home_id
                        else "away"
                    ),
                    "matchup": (
                        f"{away_abbr} @ {home_abbr}"
                    ),
                })

            return respond(
                start_response,
                {"games": games},
            )

        except Exception:
            return respond(
                start_response,
                {"error": "Database query failed"},
                "503 Service Unavailable",
            )

    return respond(
        start_response,
        {"error": "Not found"},
        "404 Not Found",
    )


application = app
