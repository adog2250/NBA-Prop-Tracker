# NBA Prop Tracker — postgame data collector (starter)

Independent from SimuZenSports. This starter **does not yet include the iPhone UI, cloud database, or hosting**. It uses an unofficial NBA CDN feed; availability, permission to use, and response formats can change. Verify applicable terms before deployment. No API key is needed for this initial local test.

## What works
- Checks NBA's *current-day* scoreboard for games marked final.
- Downloads a final box score for each final game, validates player records and that player points sum to team scores, and saves all active players in SQLite.
- Saves minutes, points, rebounds, assists, 3PM, steals, blocks, turnovers, field goals and free throws, opponents, dates and home/away.
- Avoids duplicate records; re-imports final games for corrections after an hour.
- `recent_games(conn, player_id, 5|10|20, 'All'|'Home'|'Away')` applies the venue filter **before** choosing the most recent N.
- Retries temporary network failures and does not publish invalid/incomplete box scores.

## Test locally (Python 3.10+)

From the project root:

```bash
python -m unittest discover -s tests -v
python -m collector.run --once
```

The first command runs offline tests. The second contacts the NBA feed and creates `data/nba.sqlite3`. If there are no completed games on today's feed, it prints `{}`. You can keep it running with `python -m collector.run --interval 60`.

## Before production deployment
1. Confirm NBA feed terms and reliability; use an authorized replacement if necessary.
2. Add a **durable cloud database** (SQLite on ephemeral hosting will lose data).
3. Add historical backfill and a schedule feed covering all game dates; the current-day feed alone cannot guarantee coverage of games across midnight or outages.
4. Add an API for the iPhone UI and refresh/invalidation after database commits.
5. Add source-specific injury status, roster and headshot integrations; they are **not** included here.
6. Add health monitoring, alerting, and validation for delayed final box scores, corrections and exceptional games.
7. Configure an always-on server or suitable scheduler; a 60-second loop does not run by itself on GitHub and free hosting may not support it.

Target is prompt, accurate postgame updates—not guaranteed 1–3 minute publishing. No live in-game player stats are collected.
