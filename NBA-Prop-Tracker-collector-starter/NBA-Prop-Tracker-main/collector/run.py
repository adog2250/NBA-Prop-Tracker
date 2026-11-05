import argparse
import logging
import os
import sqlite3
import time
from pathlib import Path
from collector.core import init_db, sync


def main():
    parser = argparse.ArgumentParser(description='NBA Prop Tracker postgame collector')
    parser.add_argument('--once', action='store_true', help='Run one check and exit')
    parser.add_argument('--force', action='store_true', help='Recheck finals for stat corrections')
    parser.add_argument('--interval', type=int, default=60, help='Seconds between checks')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    db = Path(os.getenv('NBA_DB_PATH', 'data/nba.sqlite3'))
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    init_db(conn)
    while True:
        try:
            print(sync(conn, force=args.force), flush=True)
        except Exception:
            logging.exception('Collector cycle failed; will retry')
        if args.once:
            break
        time.sleep(max(30, args.interval))


if __name__ == '__main__':
    main()
