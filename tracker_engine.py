import time
import threading
import os
import config_manager
import hunt_detector
import logger
from log_tailer import LogTailer, DynamicNvidiaLogTailer
from combat_state_machine import CombatStateMachine
from network_sync import NetworkSync

log = logger.get_logger("Engine")


class TrackerEngine:
    def __init__(self, on_session_change=None, on_combat_event=None):
        self.on_session_change = on_session_change
        self.on_combat_event = on_combat_event

        self.running = False
        self.thread = None
        self.network_sync = NetworkSync()

        self.game_tailer = None
        self.nvidia_tailer = None
        self.current_hunt_log_path = None

        self.state_machine = CombatStateMachine(
            on_session_start=self._handle_session_start,
            on_session_end=self._handle_session_end,
            on_event=self._handle_combat_event
        )

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        try:
            self.network_sync.stop()
        except Exception:
            pass

    def _warmup_lobby_mode(self, log_path):
        """Быстро пробегает лог с начала для точного восстановления активного лобби."""
        if not log_path or not os.path.exists(log_path):
            return
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if "DeepLinkingTarget" in line or "SelectDeepLinkingTarget" in line or "[LocalMission" in line:
                        self.state_machine.feed_game_log_line(line)
        except Exception:
            pass

    def _ensure_tailers(self):
        config = config_manager.load_config()
        custom_path = config.get("custom_hunt_path")

        # 1. Проверяем Game.log
        _, game_log, status = hunt_detector.detect_hunt_directory(custom_path)
        if status == 'ok' and game_log:
            if game_log != self.current_hunt_log_path or self.game_tailer is None:
                self.current_hunt_log_path = game_log
                self._warmup_lobby_mode(game_log)
                # При старте встаем в конец файла, чтобы не парсить вчерашние матчи
                self.game_tailer = LogTailer(game_log, start_at_end=True)
        else:
            self.game_tailer = None
            self.current_hunt_log_path = None

        # 2. Проверяем NVIDIA log
        if self.nvidia_tailer is None:
            self.nvidia_tailer = DynamicNvidiaLogTailer(hunt_detector.detect_nvidia_log, start_at_end=True)

    def _recover_interrupted_match_if_any(self):
        """Проверяет наличие аварийно прерванного матча после падения/закрытия программы."""
        saved = config_manager.load_recovery_state()
        if not saved:
            return
        kills = int(saved.get("kills", 0))
        deaths = int(saved.get("deaths", 0))
        match_id = saved.get("match_id")
        if (kills > 0 or deaths > 0) and match_id:
            log.info(f"Обнаружен незавершенный матч после сбоя: {match_id} (K={kills}, D={deaths}). Отправка в базу...")
            try:
                self.network_sync.send_match_pulse(saved)
            except Exception as e:
                log.error(f"Не удалось отправить восстановленный матч: {e}")
        config_manager.clear_recovery_state()

    def _worker_loop(self):
        # 0. Проверяем и восстанавливаем оборванный матч при предыдущем падении
        self._recover_interrupted_match_if_any()

        # Первичная инициализация источников
        self._ensure_tailers()

        check_counter = 0
        while self.running:
            try:
                # Каждые ~3 секунды перепроверяем доступность путей (на случай рестарта игры)
                check_counter += 1
                if check_counter >= 20:
                    check_counter = 0
                    self._ensure_tailers()

                # Читаем новые строки Game.log
                if self.game_tailer:
                    new_game_lines = self.game_tailer.read_new_lines()
                    for line in new_game_lines:
                        self.state_machine.feed_game_log_line(line)

                # Читаем новые строки NVIDIA GfeSDK
                if self.nvidia_tailer:
                    new_nv_lines = self.nvidia_tailer.read_new_lines()
                    for line in new_nv_lines:
                        self.state_machine.feed_nvidia_line(line)

            except Exception:
                pass

            time.sleep(0.15)

    def _handle_session_start(self, map_name, match_id):
        log.info(f"Начало сессии матча: Карта '{map_name}', ID={match_id}")
        # Сохраняем состояние начавшегося матча на случай аварии / вылета
        config_manager.save_recovery_state({
            "match_id": match_id,
            "map": map_name,
            "game_mode": self.state_machine.current_game_mode,
            "kills": 0,
            "deaths": 0,
            "trades": 0,
            "duration_seconds": 0,
            "start_time": time.time()
        })
        try:
            self.network_sync.send_match_state_pulse("in_combat", 0, 0)
        except Exception:
            pass
        if self.on_session_change:
            try:
                self.on_session_change(True, {
                    "map": map_name,
                    "match_id": match_id,
                    "kills": 0,
                    "deaths": 0,
                    "start_time": time.time()
                })
            except Exception:
                pass

    def _handle_session_end(self, summary):
        kills = int(summary.get("kills", 0))
        deaths = int(summary.get("deaths", 0))
        duration = int(summary.get("duration_seconds", 0))
        log.info(f"Окончание матча: Киллы={kills}, Смерти={deaths}, Длительность={duration}с")

        # Штатный выход — очищаем файл аварийного восстановления
        config_manager.clear_recovery_state()

        # Защита от спама нулевыми матчами (реконнекты Crytek, сбои, Alt+F4):
        # Если матч пустой (0 киллов и 0 смертей), в базу данных его не отправляем
        if kills == 0 and deaths == 0:
            log.info("Матч завершился без фрагов и смертей (0K, 0D). Пропуск отправки в базу данных.")
            try:
                self.network_sync.send_match_state_pulse("in_lobby", 0, 0)
            except Exception as e:
                log.error(f"Ошибка сброса match_state в in_lobby: {e}")

            if self.on_session_change:
                try:
                    self.on_session_change(False, summary)
                except Exception:
                    pass
            return

        # 1. Отправляем импульс завершения матча (сохранение в базе)
        try:
            ok, res = self.network_sync.send_match_pulse(summary)
            if ok and isinstance(res, dict):
                summary.update(res)
        except Exception as e:
            log.error(f"Ошибка при передаче матча в NetworkSync: {e}")

        # 2. Немедленно сбрасываем статус сессии в лобби (обнуление таймера матча на сайте)
        try:
            self.network_sync.send_match_state_pulse("in_lobby", 0, 0)
        except Exception as e:
            log.error(f"Ошибка сброса match_state в in_lobby: {e}")

        if self.on_session_change:
            try:
                self.on_session_change(False, summary)
            except Exception:
                pass

    def _handle_combat_event(self, event_type, count, current_stats):
        log.info(f"Боевое событие: {event_type} x{count}. Текущий счет: {current_stats}")
        # Обновляем состояние на диске для надежного восстановления при краше
        config_manager.save_recovery_state({
            "match_id": current_stats.get("match_id"),
            "map": current_stats.get("map"),
            "game_mode": current_stats.get("game_mode"),
            "kills": current_stats.get("kills", 0),
            "deaths": current_stats.get("deaths", 0),
            "trades": current_stats.get("trades", 0),
            "duration_seconds": current_stats.get("duration", 0),
            "start_time": time.time() - current_stats.get("duration", 0)
        })
        try:
            self.network_sync.send_match_state_pulse(
                "in_combat",
                current_stats.get("kills", 0),
                current_stats.get("deaths", 0)
            )
        except Exception:
            pass
        if self.on_combat_event:
            try:
                self.on_combat_event(event_type, count, current_stats)
            except Exception:
                pass
            except Exception:
                pass
