import sys
import os
import io
import time
import shutil
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config_manager
from network_sync import NetworkSync, BACKOFF_STAGES, MAX_ATTEMPTS, MAX_RETRY_AFTER, DEFAULT_RETRY_AFTER


def make_http_error(code, body, headers=None):
    return urllib.error.HTTPError(
        "http://localhost:3000/api/tracker/pulse",
        code,
        "error",
        headers or {},
        io.BytesIO(body.encode("utf-8")),
    )


class TestDeliveryErrors(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig = (
            config_manager.APP_DATA_DIR,
            config_manager.CONFIG_FILE,
            config_manager.RECOVERY_FILE,
            config_manager.PENDING_MATCHES_FILE,
        )
        config_manager.APP_DATA_DIR = self.temp_dir
        config_manager.CONFIG_FILE = os.path.join(self.temp_dir, "config.json")
        config_manager.RECOVERY_FILE = os.path.join(self.temp_dir, "match_recovery.json")
        config_manager.PENDING_MATCHES_FILE = os.path.join(self.temp_dir, "pending_matches.json")
        config_manager.save_config({
            "sync_token": "test_token_12345",
            "api_base_url": "http://localhost:3000",
        })

    def tearDown(self):
        (config_manager.APP_DATA_DIR,
         config_manager.CONFIG_FILE,
         config_manager.RECOVERY_FILE,
         config_manager.PENDING_MATCHES_FILE) = self.orig
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _sync_with_item(self, event_id="m1"):
        sync = NetworkSync()
        item = {"event_id": event_id, "type": "match", "created_at": time.time(), "kills": 1}
        sync.retry_queue = [item]
        sync._persist_queue()
        return sync, item

    # --- 401/403: постоянная ошибка токена -----------------------------------
    def test_401_stops_delivery_and_keeps_match(self):
        sync, item = self._sync_with_item()
        try:
            sync._handle_delivery_error(item, {"token_error": True, "error": "Недействительный токен", "status": 401})
            state = sync.get_delivery_state()
            self.assertTrue(state["token_error"])
            self.assertEqual(state["token_error_code"], 401)
            self.assertEqual(len(sync.retry_queue), 1, "Матч не должен удаляться при 401")
            self.assertEqual(len(config_manager.load_pending_matches()), 1)
            self.assertFalse(sync.retry_queue[0].get("permanent_fail"), "401 — не permanent, матч ждёт исправления токена")
        finally:
            sync.stop()

    def test_403_stops_delivery(self):
        sync, item = self._sync_with_item()
        try:
            sync._handle_delivery_error(item, {"token_error": True, "error": "Доступ запрещён", "status": 403})
            self.assertTrue(sync.get_delivery_state()["token_error"])
            self.assertEqual(len(sync.retry_queue), 1)
        finally:
            sync.stop()

    # --- прочие 4xx: постоянная ошибка конкретного матча ---------------------
    def test_400_generic_marks_permanent(self):
        sync, item = self._sync_with_item()
        try:
            sync._handle_delivery_error(item, {"permanent": True, "error": "Некорректный запрос", "status": 400})
            self.assertTrue(sync.retry_queue[0].get("permanent_fail"))
            self.assertEqual(sync.retry_queue[0].get("fail_code"), 400)
            self.assertEqual(sync.get_delivery_state()["failed_count"], 1)
            self.assertEqual(sync.backoff_idx, 0, "Постоянная ошибка не должна крутить бэкофф")
        finally:
            sync.stop()

    # --- 429: пауза по Retry-After ------------------------------------------
    def test_429_honours_retry_after(self):
        sync, item = self._sync_with_item()
        try:
            sync._handle_delivery_error(item, {"rate_limited": True, "retry_after": 120, "error": "Слишком много запросов", "status": 429})
            self.assertEqual(sync.next_delay, 120)
            self.assertEqual(sync.backoff_idx, 0, "429 не должен наращивать бэкофф")
            self.assertFalse(sync.retry_queue[0].get("permanent_fail"))
        finally:
            sync.stop()

    def test_429_without_header_uses_default(self):
        sync, item = self._sync_with_item()
        try:
            sync._handle_delivery_error(item, {"rate_limited": True, "retry_after": None, "status": 429})
            self.assertEqual(sync.next_delay, DEFAULT_RETRY_AFTER)
        finally:
            sync.stop()

    def test_429_caps_retry_after(self):
        sync, item = self._sync_with_item()
        try:
            sync._handle_delivery_error(item, {"rate_limited": True, "retry_after": 999999, "status": 429})
            self.assertEqual(sync.next_delay, MAX_RETRY_AFTER)
        finally:
            sync.stop()

    # --- 5xx и сеть: обычный бэкофф -----------------------------------------
    def test_500_and_network_keep_backoff(self):
        sync, item = self._sync_with_item()
        try:
            sync._handle_delivery_error(item, {"is_server_error": True, "error": "HTTP 500", "status": 500})
            self.assertEqual(sync.backoff_idx, 1)
            self.assertFalse(sync.retry_queue[0].get("permanent_fail"))
            sync._handle_delivery_error(item, {"is_network_error": True, "error": "timeout"})
            self.assertEqual(sync.backoff_idx, 2)
            self.assertFalse(sync.retry_queue[0].get("permanent_fail"))
        finally:
            sync.stop()

    # --- лимит попыток -------------------------------------------------------
    def test_max_attempts_marks_permanent(self):
        sync, item = self._sync_with_item()
        try:
            for _ in range(MAX_ATTEMPTS):
                sync._handle_delivery_error(item, {"is_network_error": True, "error": "timeout"})
            self.assertTrue(sync.retry_queue[0].get("permanent_fail"))
            self.assertEqual(sync.get_delivery_state()["failed_count"], 1)
        finally:
            sync.stop()

    # --- ручной повтор -------------------------------------------------------
    def test_retry_failed_clears_state(self):
        sync, item = self._sync_with_item()
        try:
            sync._handle_delivery_error(item, {"token_error": True, "error": "Недействительный токен", "status": 401})
            sync._handle_delivery_error(item, {"permanent": True, "error": "Некорректный запрос", "status": 422})
            self.assertTrue(sync.get_delivery_state()["token_error"])
            self.assertEqual(sync.get_delivery_state()["failed_count"], 1)

            state = sync.retry_failed()
            self.assertFalse(state["token_error"])
            self.assertEqual(state["failed_count"], 0)
            self.assertFalse(sync.retry_queue[0].get("permanent_fail"))
            self.assertEqual(len(config_manager.load_pending_matches()), 1)
        finally:
            sync.stop()

    # --- разбор HTTP-кодов в _post_payload -----------------------------------
    def test_post_payload_maps_401_to_token_error(self):
        sync = NetworkSync()
        try:
            err = make_http_error(401, '{"error":"Недействительный токен"}')
            with patch("urllib.request.urlopen", side_effect=err):
                ok, info = sync._post_payload({"type": "match", "event_id": "x"})
            self.assertFalse(ok)
            self.assertTrue(info.get("token_error"))
            self.assertEqual(info.get("status"), 401)
        finally:
            sync.stop()

    def test_post_payload_reads_retry_after(self):
        sync = NetworkSync()
        try:
            err = make_http_error(429, '{"error":"Слишком много запросов"}', {"Retry-After": "90"})
            with patch("urllib.request.urlopen", side_effect=err):
                ok, info = sync._post_payload({"type": "match", "event_id": "x"})
            self.assertFalse(ok)
            self.assertTrue(info.get("rate_limited"))
            self.assertEqual(info.get("retry_after"), 90)
        finally:
            sync.stop()

    def test_post_payload_maps_422_to_permanent(self):
        sync = NetworkSync()
        try:
            err = make_http_error(422, '{"error":"Некорректные данные"}')
            with patch("urllib.request.urlopen", side_effect=err):
                ok, info = sync._post_payload({"type": "match", "event_id": "x"})
            self.assertFalse(ok)
            self.assertTrue(info.get("permanent"))
        finally:
            sync.stop()

    def test_post_payload_keeps_409_manual_conflict(self):
        sync = NetworkSync()
        try:
            err = make_http_error(409, '{"error":"Ручная сессия","manual_conflict":true}')
            with patch("urllib.request.urlopen", side_effect=err):
                ok, info = sync._post_payload({"type": "session_start"})
            self.assertFalse(ok)
            self.assertTrue(info.get("manual_conflict"))
        finally:
            sync.stop()


if __name__ == "__main__":
    unittest.main()
