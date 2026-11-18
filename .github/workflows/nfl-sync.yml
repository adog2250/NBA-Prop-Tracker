name: NFL Roster Sync

on:
  workflow_dispatch:
  schedule:
    - cron: "*/5 * * * *"

permissions:
  contents: read

concurrency:
  group: nfl-roster-sync
  cancel-in-progress: false

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 15

    env:
      DATABASE_URL: ${{ secrets.DATABASE_URL }}
      PYTHONUNBUFFERED: "1"

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install database dependency
        run: python -m pip install "psycopg[binary]>=3.2,<4"

      - name: Refresh NFL teams and rosters
        run: python collector/sync_nfl_rosters.py

      - name: Refresh NFL games
        run: python collector/sync_nfl_games.py
