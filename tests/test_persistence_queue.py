import sys
import os
import time
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Добавляем родительскую директорию KD Connect в sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config_manager
from network_sync import NetworkSync, MAX_QUEUE_AGE, BACKOFF_STAGES
from tracker_engine import TrackerEngine


class TestPersistenceQueue(unittest.TestCase):
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

        # Базовый конфиг с токеном
        config_manager.save_config({
            "sync_token": "kdm_live_test_token_123",
            "api_base_url": "http://localhost:3000"
        })

    def tearDown(self):
        config_manager.APP_DATA_DIR = self.orig_app_data
        config_manager.CONFIG_FILE = self.orig_cfg_file
        config_manager.RECOVERY_FILE = self.orig_rec_file
        config_manager.PENDING_MATCHES_FILE = self.orig_pending_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Персистентная очередь на диске и отсечка записей старше 4 часов
    # -------------------------------------------------------------------------
    def test_queue_persists_to_disk_and_filters_expired(self):
        """Проверка сохранения в pending_matches.json и фильтрации старых записей (>4ч)."""
        now = time.time()
        initial_matches = [
            {"event_id": "fresh_1", "created_at": now - 3600, "kills": 2, "deaths": 1},
            {"event_id": "expired_old", "created_at": now - 15000, "kills": 5, "deaths": 0},  # > 4ч (14400с)
            {"event_id": "fresh_2", "created_at": now - 60, "kills": 1, "deaths": 0}
        ]
        config_manager.save_pending_matches(initial_matches)
        self.assertTrue(os.path.exists(config_manager.PENDING_MATCHES_FILE))

        # Запускаем NetworkSync — он должен загрузить очередь и отбросить expired_old
        sync = NetworkSync()
        try:
            pending = sync.get_pending_matches()
            event_ids = [m["event_id"] for m in pending]
            self.assertIn("fresh_1", event_ids)
            self.assertIn("fresh_2", event_ids)
            self.assertNotIn("expired_old", event_ids)
            self.assertEqual(len(pending), 2)

            # Проверяем, что файл на диске тоже обновился без устаревшей записи
            on_disk = config_manager.load_pending_matches()
            self.assertEqual(len(on_disk), 2)
            self.assertNotIn("expired_old", [m["event_id"] for m in on_disk])
        finally:
            sync.stop()

    # -------------------------------------------------------------------------
    # 2. Экспоненциальный бэкофф при сетевых ошибках
    # -------------------------------------------------------------------------
    def test_exponential_backoff_on_network_error(self):
        """Проверка ступеней бэкоффа [3, 6, 12, 30] только для сетевых ошибок и сброса при успехе."""
        sync = NetworkSync()
        try:
            # Имитируем сбой сети
            item = {"event_id": "m1", "type": "match", "created_at": time.time()}
            sync._handle_delivery_error(item, {"is_network_error": True, "error": "Connection refused"})
            self.assertEqual(sync.backoff_idx, 1)  # Первый сбой -> следующая задержка 6с

            sync._handle_delivery_error(item, {"is_server_error": True, "error": "HTTP 502"})
            self.assertEqual(sync.backoff_idx, 2)  # Второй сбой -> следующая задержка 12с

            sync._handle_delivery_error(item, {"is_server_error": True, "error": "HTTP 500"})
            self.assertEqual(sync.backoff_idx, 3)  # Третий сбой -> следующая задержка 30с

            sync._handle_delivery_error(item, {"is_network_error": True, "error": "Timeout"})
            self.assertEqual(sync.backoff_idx, 3)  # Достигнут максимум 30с

            # При логической ошибке (например, нет сессии) бэкофф сбрасывается в 0
            sync._handle_delivery_error(item, {"no_active_session": True, "error": "нет активной сессии"})
            self.assertEqual(sync.backoff_idx, 0)
        finally:
            sync.stop()

    # -------------------------------------------------------------------------
    # 3. Обработка manual_conflict (409)
    # -------------------------------------------------------------------------
    def test_manual_conflict_pauses_without_deleting_match(self):
        """При manual_conflict отправка приостанавливается, матч остаётся в очереди."""
        sync = NetworkSync()
        try:
            item = {"event_id": "conflict_match_1", "type": "match", "created_at": time.time(), "kills": 3}
            sync.retry_queue = [item]
            sync._persist_queue()

            # Имитируем ответ сервера manual_conflict
            sync._handle_delivery_error(item, {"manual_conflict": True, "status": 409})

            self.assertTrue(sync.manual_conflict)
            self.assertEqual(len(sync.retry_queue), 1, "Матч НЕ должен удаляться при manual_conflict!")
            self.assertEqual(sync.retry_queue[0]["event_id"], "conflict_match_1")
            self.assertEqual(len(config_manager.load_pending_matches()), 1)
        finally:
            sync.stop()

    # -------------------------------------------------------------------------
    # 4. Обработка session_mismatch (409)
    # -------------------------------------------------------------------------
    def test_session_mismatch_deletes_only_rejected_match(self):
        """При session_mismatch удаляется только этот матч, остальные остаются."""
        sync = NetworkSync()
        try:
            item_bad = {"event_id": "mismatch_match", "session_id": "old_session_1", "created_at": time.time()}
            item_good = {"event_id": "valid_match", "session_id": "current_session_2", "created_at": time.time()}
            sync.retry_queue = [item_bad, item_good]
            sync._persist_queue()

            # Имитируем ответ session_mismatch для первого матча
            sync._handle_delivery_error(item_bad, {"session_mismatch": True, "status": 409})

            self.assertEqual(len(sync.retry_queue), 1)
            self.assertEqual(sync.retry_queue[0]["event_id"], "valid_match", "Только несовпадающий матч должен быть удален!")
            on_disk = config_manager.load_pending_matches()
            self.assertEqual(len(on_disk), 1)
            self.assertEqual(on_disk[0]["event_id"], "valid_match")
        finally:
            sync.stop()

    # -------------------------------------------------------------------------
    # 5. Обработка duplicate_prevented (200)
    # -------------------------------------------------------------------------
    def test_duplicate_prevented_treated_as_delivered(self):
        """Ответ duplicate_prevented считается успешным, матч удаляется из очереди."""
        sync = NetworkSync()
        try:
            item = {"event_id": "dup_1", "created_at": time.time()}
            sync.retry_queue = [item]
            sync._persist_queue()

            # Мокаем успешный ответ сервера с duplicate_prevented
            with patch.object(sync, "_post_payload", return_value=(True, {"success": True, "duplicate_prevented": True})):
                remaining = sync.flush_pending(timeout=2.0)
                self.assertEqual(remaining, 0)
                self.assertEqual(len(sync.retry_queue), 0)
                self.assertEqual(len(config_manager.load_pending_matches()), 0)
        finally:
            sync.stop()

    # -------------------------------------------------------------------------
    # 6. Flush очереди перед закрытием сессии
    # -------------------------------------------------------------------------
    def test_flush_pending_with_timeout(self):
        """flush_pending доставляет доступные матчи или возвращает остаток."""
        sync = NetworkSync()
        try:
            m1 = {"event_id": "f1", "created_at": time.time()}
            m2 = {"event_id": "f2", "created_at": time.time()}
            sync.retry_queue = [m1, m2]
            sync._persist_queue()

            # Мокаем успешную доставку обоих матчей
            with patch.object(sync, "_post_payload", return_value=(True, {"success": True})):
                remaining = sync.flush_pending(timeout=3.0)
                self.assertEqual(remaining, 0)
                self.assertEqual(len(sync.retry_queue), 0)
                self.assertEqual(len(config_manager.load_pending_matches()), 0)
        finally:
            sync.stop()

    # -------------------------------------------------------------------------
    # 7. Порядок операций в TrackerEngine: сохранение до очистки recovery
    # -------------------------------------------------------------------------
    def test_tracker_engine_enqueues_before_clearing_recovery(self):
        """Проверка: матч сохраняется в pending_matches.json до очистки match_recovery.json."""
        order_of_calls = []

        def mock_send(summary):
            order_of_calls.append("send_match_pulse")
            # Проверяем, что в момент отправки recovery state еще существует
            self.assertTrue(os.path.exists(config_manager.RECOVERY_FILE))
            return True, {"success": True}

        def mock_clear():
            order_of_calls.append("clear_recovery_state")
            if os.path.exists(config_manager.RECOVERY_FILE):
                os.remove(config_manager.RECOVERY_FILE)

        engine = TrackerEngine()
        try:
            # Создаем файл recovery
            config_manager.save_recovery_state({"match_id": "test_mid", "kills": 2, "deaths": 1})
            self.assertTrue(os.path.exists(config_manager.RECOVERY_FILE))

            with patch.object(engine.network_sync, "send_match_pulse", side_effect=mock_send), \
                 patch("config_manager.clear_recovery_state", side_effect=mock_clear):
                summary = {"match_id": "test_mid", "kills": 2, "deaths": 1, "duration_seconds": 300}
                engine._handle_session_end(summary)

            self.assertEqual(order_of_calls, ["send_match_pulse", "clear_recovery_state"])
        finally:
            engine.stop()

    def test_recovery_file_kept_on_send_match_pulse_exception(self):
        """Проверка O1: при исключении в send_match_pulse match_recovery.json НЕ удаляется."""
        engine = TrackerEngine()
        try:
            # Создаем файл recovery
            config_manager.save_recovery_state({"match_id": "mid_crash", "kills": 3, "deaths": 0})
            self.assertTrue(os.path.exists(config_manager.RECOVERY_FILE))

            # Имитируем падение send_match_pulse с неожиданным исключением
            with patch.object(engine.network_sync, "send_match_pulse", side_effect=RuntimeError("Disk write failed")):
                summary = {"match_id": "mid_crash", "kills": 3, "deaths": 0, "duration_seconds": 120}
                engine._handle_session_end(summary)

            # Файл восстановления ОБЯЗАН остаться на диске!
            self.assertTrue(os.path.exists(config_manager.RECOVERY_FILE), "match_recovery.json должен остаться на месте!")
            saved = config_manager.load_recovery_state()
            self.assertIsNotNone(saved)
            self.assertEqual(saved.get("match_id"), "mid_crash")
        finally:
            engine.stop()

    # -------------------------------------------------------------------------
    # 8. Стресс-тест: убийство процесса с очередью -> перезапуск -> доставка
    # -------------------------------------------------------------------------
    def test_process_crash_and_restart_delivers_pending_queue(self):
        """Стресс-тест: краш трекера с неотправленной очередью -> рестарт -> сохранение на диске -> доставка."""
        # 1. Первая сессия трекера (сеть недоступна)
        sync1 = NetworkSync()
        try:
            summary = {
                "match_id": "crash_test_match_99",
                "kills": 4,
                "deaths": 1,
                "trades": 1,
                "duration_seconds": 650,
                "game_mode": "BountyHuntLobby"
            }
            # Имитируем сбой сети при попытке немедленной отправки
            with patch.object(sync1, "_post_payload", return_value=(False, {"is_network_error": True})):
                sync1.send_match_pulse(summary)

            # Проверяем, что матч остался на диске
            on_disk = config_manager.load_pending_matches()
            self.assertEqual(len(on_disk), 1)
            self.assertEqual(on_disk[0]["event_id"], "crash_test_match_99")
            self.assertEqual(on_disk[0]["game_mode"], "bounty_hunt")  # Проверка нормализации
        finally:
            # 2. Имитируем завершение процесса (убийство)
            sync1.stop()

        # Файл pending_matches.json остался сохранённым на диске
        self.assertTrue(os.path.exists(config_manager.PENDING_MATCHES_FILE))

        # 3. Рестарт приложения: запускается новый экземпляр NetworkSync
        sync2 = NetworkSync()
        try:
            # Проверяем, что новый экземпляр подхватил матч из файла
            self.assertEqual(len(sync2.retry_queue), 1)
            self.assertEqual(sync2.retry_queue[0]["event_id"], "crash_test_match_99")

            # 4. Восстановление сети: эмулируем успешную доставку
            with patch.object(sync2, "_post_payload", return_value=(True, {"success": True})):
                rem = sync2.flush_pending(timeout=3.0)
                self.assertEqual(rem, 0)
                self.assertEqual(len(sync2.retry_queue), 0)
                # Файл на диске очищен после успешной доставки
                self.assertEqual(len(config_manager.load_pending_matches()), 0)
        finally:
            sync2.stop()


if __name__ == "__main__":
    unittest.main()
