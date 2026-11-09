"""NBA Prop Tracker: real saved game logs, approved compact UI, and PWA assets."""
import json
import logging
import mimetypes
import os
from datetime import datetime, date
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo
import psycopg

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'

def database():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5, sslmode='require')

def respond(start, data, status='200 OK', content_type='application/json; charset=utf-8'):
    body = data if isinstance(data, bytes) else json.dumps(data, default=str).encode()
    start(status, [('Content-Type', content_type), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff')])
    return [body]

def season_bounds(day):
    year = day.year if day.month >= 10 else day.year - 1
    return date(year, 10, 1), date(year + 1, 7, 1)

def dashboard(day):
    first, last = season_bounds(day)
    with database() as conn, conn.cursor() as cur:
        cur.execute('''SELECT g.id, ht.id, ht.abbreviation, at.id, at.abbreviation, g.status
          FROM games g JOIN teams ht ON ht.id=g.home_team_id JOIN teams at ON at.id=g.away_team_id
          WHERE g.game_date=%s ORDER BY g.id''', (day,))
        games = [dict(zip(('id','home_id','home','away_id','away','status'), r)) for r in cur.fetchall()]
        cur.execute('''SELECT p.id,p.full_name,p.current_team_id,t.abbreviation
          FROM players p LEFT JOIN teams t ON t.id=p.current_team_id
          WHERE p.is_active=TRUE ORDER BY p.full_name''')
        players = {r[0]: {'id':r[0], 'name':r[1], 'team_id':r[2], 'team':r[3] or '', 'logs':[]} for r in cur.fetchall()}
        # Read optional new column through JSON so the UI remains readable before migration.
        cur.execute('''SELECT s.player_id,g.id,g.game_date,s.team_id,g.home_team_id,
          ht.abbreviation,at.abbreviation,s.minutes,s.points,s.rebounds,s.assists,
          s.threes_made,s.steals,s.blocks,
          (to_jsonb(s)->>'threes_attempted')::integer,g.updated_at
          FROM player_game_stats s JOIN games g ON g.id=s.game_id
          JOIN teams ht ON ht.id=g.home_team_id JOIN teams at ON at.id=g.away_team_id
          JOIN players p ON p.id=s.player_id
          WHERE p.is_active=TRUE AND g.status='final' AND g.game_date >= %s
            AND g.game_date < %s AND g.game_date <= %s
          ORDER BY g.game_date DESC,g.id DESC''', (first,last,day))
        updated = None
        for r in cur.fetchall():
            pid,gid,dt,tid,hid,home,away,mins,pts,reb,ast,threes,stl,blk,attempts,stamp = r
            if pid not in players: continue
            if mins is None or float(mins) <= 0: continue
            g={'id':str(gid),'date':dt.isoformat(),'home':tid==hid,'opp':away if tid==hid else home,
               'MIN':float(mins),'PTS':pts,'REB':reb,'AST':ast,'3PM':threes,'3PA':attempts,'STL':stl,'BLK':blk}
            players[pid]['logs'].append(g)
            if stamp and (updated is None or stamp > updated): updated=stamp
    return {'date':day.isoformat(),'season':f'{first.year}–{last.year%100:02d}', 'games':games,
            'players':list(players.values()),'updated_at':updated}

def app(environ, start_response):
    path=environ.get('PATH_INFO','/')
    if environ.get('REQUEST_METHOD','GET') != 'GET':
        return respond(start_response, {'error':'Method not allowed'},'405 Method Not Allowed')
    files={'/':'index.html','/manifest.webmanifest':'manifest.webmanifest','/service-worker.js':'service-worker.js',
           '/icon-192.png':'icon-192.png','/icon-512.png':'icon-512.png','/apple-touch-icon.png':'apple-touch-icon.png'}
    if path in files:
        file=STATIC/files[path]
        types={'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.webmanifest':'application/manifest+json'}
        if file.is_file(): return respond(start_response,file.read_bytes(),content_type=types.get(file.suffix,mimetypes.guess_type(str(file))[0] or 'application/octet-stream'))
    try:
        if path=='/health':
            with database() as conn, conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM teams'); total=cur.fetchone()[0]
            return respond(start_response,{'status':'online','database':'connected','teams_in_database':total})
        if path=='/api/dashboard':
            value=parse_qs(environ.get('QUERY_STRING','')).get('date',[None])[0]
            try: day=date.fromisoformat(value) if value else datetime.now(ZoneInfo('America/Chicago')).date()
            except ValueError: return respond(start_response,{'error':'Invalid date'},'400 Bad Request')
            return respond(start_response,dashboard(day))
    except Exception:
        logging.exception('NBA request failed')
        return respond(start_response,{'error':'Saved NBA data is temporarily unavailable. Try again shortly.'},'503 Service Unavailable')
    return respond(start_response,{'error':'Not found'},'404 Not Found')

application=app
