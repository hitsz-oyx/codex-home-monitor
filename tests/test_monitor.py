import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import HomeReader, Monitor, WatchdogManager, _is_capacity_error, discover_homes


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".codex_test"
        self.home.mkdir()
        self._make_state()
        self._make_goals()
        self._make_history()

    def tearDown(self):
        self.temp.cleanup()

    def _make_state(self):
        conn = sqlite3.connect(self.home / "state_5.sqlite")
        conn.executescript("""
          CREATE TABLE threads (
            id TEXT PRIMARY KEY, title TEXT, first_user_message TEXT, preview TEXT,
            cwd TEXT, model TEXT, updated_at_ms INTEGER, created_at_ms INTEGER,
            archived INTEGER DEFAULT 0
          );
          INSERT INTO threads VALUES ('thread-a','Newest task','Fix it','Fix it now','/repo/a','gpt-test',2000000000000,1990000000000,0);
          INSERT INTO threads VALUES ('thread-b','Older task','Document it','Document it','/repo/b','gpt-test',1900000000000,1890000000000,0);
        """)
        conn.commit(); conn.close()

    def _make_goals(self):
        conn = sqlite3.connect(self.home / "goals_1.sqlite")
        conn.executescript("""
          CREATE TABLE thread_goals (
            thread_id TEXT PRIMARY KEY, goal_id TEXT, objective TEXT, status TEXT,
            token_budget INTEGER, tokens_used INTEGER, time_used_seconds INTEGER,
            created_at_ms INTEGER, updated_at_ms INTEGER
          );
          INSERT INTO thread_goals VALUES ('thread-a','goal-a','Make it pass','active',1000,20,4,1990000000000,2000000000000);
        """)
        conn.commit(); conn.close()

    def _make_history(self):
        conn = sqlite3.connect(self.home / "thread_history_1.sqlite")
        conn.executescript("""
          CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER, error_json TEXT);
          CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, created_at_ms INTEGER, item_type TEXT, item_json TEXT);
          INSERT INTO thread_turns VALUES ('thread-a','turn-a','in_progress',2000000000,NULL,NULL,NULL);
          INSERT INTO thread_items VALUES ('thread-a','turn-a','item-a',2000000000000,'userMessage','{"type":"userMessage","content":[{"type":"text","text":"Please inspect this"}]}');
          INSERT INTO thread_items VALUES ('thread-a','turn-a','item-b',2000000001000,'agentMessage','{"type":"agentMessage","text":"I will inspect it"}');
          INSERT INTO thread_items VALUES ('thread-a','turn-a','item-c',2000000002000,'commandExecution','{"type":"commandExecution","command":"echo $OPENAI_API_KEY","aggregatedOutput":"done","status":"completed"}');
        """)
        conn.commit(); conn.close()

    def test_sessions_join_goal_and_sort_by_updated_time(self):
        sessions = HomeReader(self.home).sessions()
        self.assertEqual([item['thread_id'] for item in sessions], ['thread-a', 'thread-b'])
        self.assertEqual(sessions[0]['goal_status'], 'active')
        self.assertEqual(sessions[0]['activity'], 'working')
        self.assertIsNone(sessions[1]['goal'])

    def test_monitor_filters_by_goal(self):
        monitor = Monitor([self.home])
        sessions = monitor.sessions({'goal_status': ['none'], 'q': ['older'], 'limit': ['5']})
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['thread_id'], 'thread-b')

    def test_detail_does_not_return_raw_item_json(self):
        detail = HomeReader(self.home).session_detail('thread-a')
        self.assertEqual(detail['events'][0]['item_type'], 'userMessage')
        self.assertEqual([item['kind'] for item in detail['content']], ['message', 'message', 'command'])
        self.assertEqual(detail['content'][0]['text'], 'Please inspect this')
        self.assertEqual(detail['content'][2]['command'], 'echo $OPENAI_API_KEY')
        self.assertNotIn('item_json', json.dumps(detail))

    def test_detail_returns_latest_page_and_cursor(self):
        latest = HomeReader(self.home).session_detail('thread-a', content_limit=2)
        self.assertTrue(latest['has_more'])
        self.assertEqual(len(latest['content']), 2)
        older = HomeReader(self.home).session_detail('thread-a', content_limit=2, before_created_at_ms=latest['next_before_created_at_ms'])
        self.assertEqual(len(older['content']), 1)
        self.assertFalse(older['has_more'])

    def test_watchdog_only_matches_transient_capacity(self):
        self.assertTrue(_is_capacity_error('{"codexErrorInfo":"serverOverloaded","message":"Selected model is at capacity"}'))
        self.assertFalse(_is_capacity_error('{"codexErrorInfo":"usageLimitExceeded","message":"You hit your usage limit"}'))
        self.assertFalse(_is_capacity_error('{"code":"context_length_exceeded"}'))

    def test_watchdog_ignores_failures_before_monitor_start(self):
        class Reader:
            def sessions(self):
                return [{
                    'key': 'home:old',
                    'home_id': 'home',
                    'thread_id': 'old',
                    'goal': None,
                    'latest_turn': {
                        'turn_id': 'turn-old', 'status': 'failed', 'capacity_error': True,
                        'error': 'Selected model is at capacity',
                        'last_activity_ms': int((time.time() - 3600) * 1000),
                    },
                }]
        monitor = type('MonitorStub', (), {'readers': [Reader()], 'started_at': time.time()})()
        watchdog = WatchdogManager(monitor)
        watchdog.enabled = True
        watchdog.scan_once()
        self.assertEqual(watchdog.status()['active_jobs'], [])
        self.assertEqual(watchdog.status()['recent'], [])

    def test_discover_homes_requires_state_files(self):
        found = discover_homes([str(self.home), str(Path(self.temp.name) / 'missing')])
        self.assertEqual(found, [self.home.resolve()])


if __name__ == '__main__':
    unittest.main()
