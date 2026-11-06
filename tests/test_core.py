import sqlite3
import unittest
from collector.core import init_db, parse_minutes, recent_games, save_final


def fixture():
    teams=[]
    for team,score in [('HME',50),('AWY',50)]:
        players=[]
        for i in range(5):
            players.append({'personId':1000+(0 if team=='HME' else 10)+i,'firstName':'Test','familyName':str(i),
                            'played':True,'statistics':{'minutes':'20:00','points':10,'reboundsTotal':4,
                            'assists':2,'threePointersMade':0,'steals':1,'blocks':0,'turnovers':1,
                            'fieldGoalsMade':5,'fieldGoalsAttempted':8,'freeThrowsMade':0,'freeThrowsAttempted':0}})
        teams.append({'teamTricode':team,'score':score,'players':players})
    return {'gameId':'123','gameStatus':3,'gameTimeUTC':'2026-11-05T02:00:00Z','homeTeam':teams[0],'awayTeam':teams[1]}


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        init_db(self.conn)

    def test_minutes(self):
        self.assertAlmostEqual(parse_minutes('34:30'),34.5)

    def test_save_and_upsert(self):
        game=fixture()
        self.assertEqual(save_final(self.conn,game),10)
        self.assertEqual(save_final(self.conn,game),10)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM player_games').fetchone()[0],10)

    def test_no_partial_publish(self):
        game=fixture()
        game['homeTeam']['players'][0]['statistics']['points']=9
        with self.assertRaises(ValueError):
            save_final(self.conn,game)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM games').fetchone()[0],0)

    def test_filters(self):
        save_final(self.conn,fixture())
        self.assertEqual(len(recent_games(self.conn,1000,5,'Home')),1)
        self.assertEqual(len(recent_games(self.conn,1000,5,'Away')),0)


if __name__=='__main__':
    unittest.main()
