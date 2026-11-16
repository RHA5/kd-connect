"""
Unit tests for Stage 15 (KD Connect session autostart).
"""
import unittest
import os
import sys
import tempfile
import shutil
import json
import io
import time
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import config_manager
import main
from network_sync import NetworkSync


class TestAutostart(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_app_data = config_manager.APP_DATA_DIR
        self.orig_cfg_file = config_manager.CONFIG_FILE
        self.orig_rec_file = config_manager.RECOVERY_FILE
        self.orig_pending_file = config_manager.PENDING_MATCHES_FILE

        config_manager.APP_DATA_DIR = self.temp_dir
        config_manager.CONFIG_FILE = os.path.join(self.temp_dir, "config.json")
        config_manager.RECOVERY_FILE = os.path.join(self.temp_dir, "match_recovery.json")
        config_manager.PENDING_MATCHES_FILE = os.path.join(self.temp_dir, "pending_matches.json")

        config_manager.save_config({
            "sync_token": "test_token_12345",
            "api_base_url": "https://kdmetrika.ru",
            "auto_start_session": True
        })
        self.net = NetworkSync()
        self.api = main.AppApi()
        self.api.engine = MagicMock()
        self.api.engine.state_machine.session_active = False
        self.api.engine.network_sync = self.net
        main._global_api = self.api

    def tearDown(self):
        self.net.running = False
        main._global_api = None
        config_manager.APP_DATA_DIR = self.orig_app_data
        config_manager.CONFIG_FILE = self.orig_cfg_file
        config_manager.RECOVERY_FILE = self.orig_rec_file
        config_manager.PENDING_MATCHES_FILE = self.orig_pending_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_config_has_auto_start_session(self):
        self.assertIn("auto_start_session", config_manager.DEFAULT_CONFIG)
        self.assertTrue(config_manager.DEFAULT_CONFIG["auto_start_session"])

    def test_autostart_payload_format(self):
        captured_payload = None

        def fake_urlopen(req, timeout=5):
            nonlocal captured_payload
            captured_payload = json.loads(req.data.decode("utf-8"))
            resp_bytes = json.dumps({
                "success": True,
                "session_id": "test-autostart-uuid",
                "base_kd": 1.5,
                "username": "tester"
            }).encode("utf-8")
            mock_resp = MagicMock()
            mock_resp.read.return_value = resp_bytes
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = self.api.start_session(mood_rating=4, retroactive_created_at="2026-11-16T12:00:00Z", auto=True)
            self.assertTrue(res.get("success"))
            self.assertIsNotNone(captured_payload)
            self.assertTrue(captured_payload.get("auto"))
            self.assertNotIn("mood_rating", captured_payload)
            self.assertEqual(captured_payload.get("created_at"), "2026-11-16T12:00:00Z")
            self.assertEqual(captured_payload.get("type"), "session_start")

    def test_autostart_triggers_once_per_match_id(self):
        start_calls = []

        def fake_start_session(mood_rating=None, retroactive_created_at=None, auto=False):
            start_calls.append({"auto": auto, "retro": retroactive_created_at})
            return {"success": True, "session_id": "sess-123"}

        self.api.start_session = fake_start_session

        data = {
            "match_id": "match-uuid-1",
            "game_mode": "BountyHunt",
            "start_time": time.time() - 30
        }
        # First trigger
        main._handle_autostart_check(data)
        self.assertEqual(len(start_calls), 1)
        self.assertTrue(start_calls[0]["auto"])

        # Second trigger with same match_id should be ignored
        main._handle_autostart_check(data)
        self.assertEqual(len(start_calls), 1)

    def test_autostart_skips_when_disabled(self):
        config_manager.update_config_value("auto_start_session", False)
        called = False

        def fake_start(*args, **kwargs):
            nonlocal called
            called = True
            return {"success": True}

        self.api.start_session = fake_start
        main._handle_autostart_check({"match_id": "m1", "game_mode": "BountyHunt", "start_time": time.time()})
        self.assertFalse(called)

    def test_autostart_skips_when_session_active_locally(self):
        self.net.set_current_session_id("existing-active-session")
        called = False

        def fake_start(*args, **kwargs):
            nonlocal called
            called = True
            return {"success": True}

        self.api.start_session = fake_start
        main._handle_autostart_check({"match_id": "m2", "game_mode": "BountyHunt", "start_time": time.time()})
        self.assertFalse(called)

    def test_autostart_skips_when_manual_conflict(self):
        self.net.set_manual_conflict(True)
        called = False

        def fake_start(*args, **kwargs):
            nonlocal called
            called = True
            return {"success": True}

        self.api.start_session = fake_start
        main._handle_autostart_check({"match_id": "m3", "game_mode": "BountyHunt", "start_time": time.time()})
        self.assertFalse(called)

    def test_autostart_skips_quickplay_and_shootingrange_and_unknown(self):
        called = False

        def fake_start(*args, **kwargs):
            nonlocal called
            called = True
            return {"success": True}

        self.api.start_session = fake_start
        for mode in ("Quickplay", "ShootingRange", "UnknownMode", ""):
            called = False
            main._handle_autostart_check({"match_id": f"m-{mode}", "game_mode": mode, "start_time": time.time()})
            self.assertFalse(called, f"Should not autostart for mode {mode}")

    def test_autostart_pending_on_network_error_and_retry_interval(self):
        attempts = 0

        def fake_start_session(mood_rating=None, retroactive_created_at=None, auto=False):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return {"success": False, "offline": True, "error": "Network down"}
            return {"success": True, "session_id": "sess-recovered"}

        self.api.start_session = fake_start_session

        data = {"match_id": "net-fail-match", "game_mode": "BountyHunt", "start_time": 1700000000}
        main._handle_autostart_check(data)
        self.assertEqual(attempts, 1)
        self.assertIsNotNone(self.api._pending_auto_start)
        self.assertEqual(self.api._pending_auto_start["match_id"], "net-fail-match")

        # Retry immediately (<15s) via check_active_session -> should not retry yet
        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"success": false}'
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            self.api.check_active_session()
            self.assertEqual(attempts, 1)

            # Move last_attempt 16s into the past
            self.api._pending_auto_start["last_attempt"] = time.time() - 16.0
            self.api.check_active_session()
            self.assertEqual(attempts, 2)
            self.assertIsNotNone(self.api._pending_auto_start)

            # Move last_attempt again -> attempt 3 succeeds
            self.api._pending_auto_start["last_attempt"] = time.time() - 16.0
            self.api.check_active_session()
            self.assertEqual(attempts, 3)
            self.assertIsNone(self.api._pending_auto_start)

    def test_preview_html_autostart_elements(self):
        preview_path = os.path.join(os.path.dirname(__file__), "..", "preview.html")
        with open(preview_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('id="session-autostart-banner"', content)
        self.assertIn('Сессия начата автоматически', content)
        self.assertIn('id="toggle-autostart"', content)
        self.assertIn('handleToggleAutostart', content)
        self.assertIn('Начинать сессию автоматически', content)
        self.assertIn('id="idle-pending-autostart-msg"', content)
        self.assertIn('Ждём связь, сессия начнётся автоматически', content)
        self.assertIn('На сайте идёт ручная сессия. Автостарт выключен до её завершения', content)


if __name__ == "__main__":
    unittest.main()
