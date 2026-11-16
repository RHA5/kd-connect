"""
Unit tests for Stage 14 (KD Connect session binding K1, reused_existing K2, offline status K3a).
"""
import unittest
import os
import sys
import tempfile
import shutil
import json
import io
from urllib.error import URLError, HTTPError
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import config_manager
import main
from network_sync import NetworkSync


class TestSessionBinding(unittest.TestCase):
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
            "sync_token": "kdm_live_test_token_12345",
            "api_base_url": "https://kdmetrika.ru"
        })
        self.net = NetworkSync()
        self.api = main.AppApi()
        self.api.engine = MagicMock()
        self.api.engine.state_machine.session_active = False
        self.api.engine.network_sync = self.net

    def tearDown(self):
        self.net.running = False
        config_manager.APP_DATA_DIR = self.orig_app_data
        config_manager.CONFIG_FILE = self.orig_cfg_file
        config_manager.RECOVERY_FILE = self.orig_rec_file
        config_manager.PENDING_MATCHES_FILE = self.orig_pending_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_send_match_state_pulse_includes_session_id_when_set(self):
        self.net.set_current_session_id("sess-xyz-789")
        # Empty any pre-existing queue items
        while not self.net.state_queue.empty():
            self.net.state_queue.get_nowait()

        self.net.send_match_state_pulse(state="in_combat", kills=2, deaths=1)
        item = self.net.state_queue.get(timeout=1.0)
        self.assertEqual(item.get("type"), "match_state")
        self.assertEqual(item.get("session_id"), "sess-xyz-789")
        self.assertEqual(item.get("state"), "in_combat")
        self.assertEqual(item.get("kills"), 2)
        self.assertEqual(item.get("deaths"), 1)

    def test_send_match_state_pulse_omits_session_id_when_none(self):
        self.net.set_current_session_id(None)
        while not self.net.state_queue.empty():
            self.net.state_queue.get_nowait()

        self.net.send_match_state_pulse(state="in_lobby", kills=0, deaths=0)
        item = self.net.state_queue.get(timeout=1.0)
        self.assertEqual(item.get("type"), "match_state")
        self.assertNotIn("session_id", item)
        self.assertEqual(item.get("state"), "in_lobby")

    def test_end_session_payload_with_session_id(self):
        self.net.set_current_session_id("sess-target-456")
        sent_requests = []

        def mock_urlopen(req, timeout=5):
            sent_requests.append(req)
            resp = MagicMock()
            resp.read.return_value = json.dumps({"success": True, "session_id": "sess-target-456"}).encode()
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = self.api.end_session(adjusted_kills=5, adjusted_deaths=2)
            self.assertTrue(res.get("success"))
            self.assertEqual(len(sent_requests), 1)
            req = sent_requests[0]
            body = json.loads(req.data.decode())
            self.assertEqual(body.get("type"), "session_end")
            self.assertEqual(body.get("session_id"), "sess-target-456")
            self.assertEqual(body.get("adjusted_kills"), 5)
            self.assertEqual(body.get("adjusted_deaths"), 2)
            # After success, current_session_id is cleared
            self.assertIsNone(self.net.get_current_session_id())

    def test_end_session_payload_without_session_id(self):
        self.net.set_current_session_id(None)
        sent_requests = []

        def mock_urlopen(req, timeout=5):
            sent_requests.append(req)
            resp = MagicMock()
            resp.read.return_value = json.dumps({"success": True}).encode()
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = self.api.end_session()
            self.assertTrue(res.get("success"))
            self.assertEqual(len(sent_requests), 1)
            req = sent_requests[0]
            body = json.loads(req.data.decode())
            self.assertNotIn("session_id", body)

    def test_end_session_already_closed(self):
        self.net.set_current_session_id("sess-already-closed")

        def mock_urlopen(req, timeout=5):
            resp = MagicMock()
            resp.read.return_value = json.dumps({
                "success": True,
                "already_closed": True,
                "session_id": "sess-already-closed"
            }).encode()
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = self.api.end_session()
            self.assertTrue(res.get("already_closed"))
            self.assertIsNone(self.net.get_current_session_id())

    def test_end_session_session_mismatch_409(self):
        self.net.set_current_session_id("sess-mismatch-123")
        err_body = json.dumps({
            "error": "Сессия уже закрыта или принадлежит другому клиенту",
            "session_mismatch": True
        }).encode()
        http_err = HTTPError(
            "https://kdmetrika.ru/api/tracker/pulse",
            409,
            "Conflict",
            {},
            io.BytesIO(err_body)
        )

        with patch("urllib.request.urlopen", side_effect=http_err):
            res = self.api.end_session()
            self.assertFalse(res.get("success"))
            self.assertTrue(res.get("session_mismatch"))
            self.assertIsNone(self.net.get_current_session_id())

    def test_handle_delivery_error_session_mismatch(self):
        self.net.set_current_session_id("sess-old-mismatch")
        self.net.session_lost = False
        item = {
            "event_id": "evt-uuid-1",
            "session_id": "sess-old-mismatch",
            "kills": 3,
            "deaths": 1
        }
        self.net.retry_queue = [item]
        resp = {"session_mismatch": True, "error": "session_mismatch"}
        self.net._handle_delivery_error(item, resp)
        self.assertIsNone(self.net.get_current_session_id())
        self.assertTrue(self.net.session_lost)
        self.assertEqual(len(self.net.retry_queue), 0)

        # consume_session_lost resets the flag
        self.assertTrue(self.net.consume_session_lost())
        self.assertFalse(self.net.consume_session_lost())

    def test_check_systems_offline_and_error_mapping(self):
        # 1. URLError -> offline
        with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
            sys_info = self.api.check_systems()
            prof = sys_info.get("profile", {})
            self.assertEqual(prof.get("status"), "offline")
            self.assertTrue(prof.get("offline"))

        # 2. HTTP 500 -> offline
        http_500 = HTTPError("https://kdmetrika.ru/api/tracker/pulse", 500, "Internal Server Error", {}, io.BytesIO(b'{"error":"crash"}'))
        with patch("urllib.request.urlopen", side_effect=http_500):
            sys_info = self.api.check_systems()
            prof = sys_info.get("profile", {})
            self.assertEqual(prof.get("status"), "offline")
            self.assertTrue(prof.get("offline"))

        # 3. HTTP 403 -> error (not offline)
        http_403 = HTTPError("https://kdmetrika.ru/api/tracker/pulse", 403, "Forbidden", {}, io.BytesIO(b'{"error":"invalid_token"}'))
        with patch("urllib.request.urlopen", side_effect=http_403):
            sys_info = self.api.check_systems()
            prof = sys_info.get("profile", {})
            self.assertEqual(prof.get("status"), "error")
            self.assertFalse(prof.get("offline"))

    def test_preview_html_elements_and_logic(self):
        preview_path = os.path.join(os.path.dirname(__file__), "..", "preview.html")
        with open(preview_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check required DOM elements
        self.assertIn('id="idle-offline-box"', content)
        self.assertIn('id="session-reused-banner"', content)
        self.assertIn('id="session-lost-banner"', content)
        self.assertIn('consume_session_lost', content)
        self.assertIn('reused_existing', content)
        self.assertIn('already_closed', content)
        self.assertIn('session_mismatch', content)
        self.assertIn('v0.10.8-beta', content)


if __name__ == "__main__":
    unittest.main()
