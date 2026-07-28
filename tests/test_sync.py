import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from cetatenie import db, sync
from cetatenie.session import Session


class SyncScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_lock_path = sync.LOCK_PATH
        db.DB_PATH = os.path.join(self.tempdir.name, "data.sqlite3")
        sync.LOCK_PATH = os.path.join(self.tempdir.name, "sync.lock")
        db.init()
        self.env = patch.dict(
            os.environ,
            {"SYNC_RETRY_MINUTES": "5", "SYNC_RETRY_MAX_MINUTES": "60"},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        db.DB_PATH = self.old_db_path
        sync.LOCK_PATH = self.old_lock_path
        self.tempdir.cleanup()

    def test_failure_records_error_and_exponential_retry(self):
        with patch.object(sync, "refresh_index", side_effect=sync.SourceAccessError("WAF blocked")):
            with self.assertRaisesRegex(sync.SourceAccessError, "WAF blocked"):
                sync.run()

        schedule = sync.schedule()
        self.assertEqual(schedule["error"], "WAF blocked")
        self.assertIsNotNone(schedule["last_attempt"])
        self.assertIsNotNone(schedule["last_failure"])
        self.assertIsNotNone(schedule["next"])
        self.assertEqual(db.get_meta(sync.FAILURE_COUNT_KEY), "1")

        failure_at = datetime.fromisoformat(db.get_meta(sync.LAST_FAILURE_KEY))
        next_at = datetime.fromisoformat(db.get_meta(sync.NEXT_SYNC_KEY))
        self.assertEqual(next_at, failure_at + timedelta(minutes=5))

    def test_success_clears_error_and_sets_regular_schedule(self):
        with patch.object(sync, "refresh_index", return_value=(10, 0)):
            self.assertEqual(sync.run(), 0)

        schedule = sync.schedule()
        self.assertIsNotNone(schedule["last"])
        self.assertIsNone(schedule["error"])
        self.assertEqual(db.get_meta(sync.FAILURE_COUNT_KEY), "0")

        last_at = datetime.fromisoformat(db.get_meta(sync.LAST_SYNC_KEY))
        next_at = datetime.fromisoformat(db.get_meta(sync.NEXT_SYNC_KEY))
        self.assertEqual(next_at, last_at + timedelta(hours=sync.interval_hours()))


class SessionTests(unittest.TestCase):
    def test_approved_proxy_is_used_for_both_protocols(self):
        with patch.dict(os.environ, {"CETATENIE_PROXY_URL": "http://proxy.example:8080"}):
            session = Session()

        self.assertEqual(session.timeout, (10.0, 60.0))
        self.assertEqual(session._session.proxies["http"], "http://proxy.example:8080")
        self.assertEqual(session._session.proxies["https"], "http://proxy.example:8080")


if __name__ == "__main__":
    unittest.main()
