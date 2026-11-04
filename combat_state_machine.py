import time
import re
import uuid
import threading

EXCLUDED_MAPS = [
    'menu',
    'levels/shooting',
    'levels/tutorial_scripted',
    'tutorial'
]

class CombatStateMachine:
    """
    Конечный автомат сессий и боевых событий Hunt: Showdown 1896.
    Сверяет сигналы Game.log и NVIDIA GfeSDK в реальном времени.
    Обеспечивает точный учет фрагов, смертей и взаимных разменов (Trades).
    """
    def __init__(self, on_session_start=None, on_session_end=None, on_event=None):
        self.on_session_start = on_session_start
        self.on_session_end = on_session_end
        self.on_event = on_event

        self.lock = threading.Lock()
        self.session_active = False
        self.current_map = None
        self.current_game_mode = "BountyHunt"  # 'BountyHunt', 'BountyClash', 'Quickplay', 'ShootingRange'
        self.match_id = None
        self.match_start_time = 0
        self.kills = 0
        self.deaths = 0
        self.trades = 0

        # Временные метки для детекции разменов и фильтрации эха
        self.last_death_time = 0
        self.last_kill_time = 0

        # Метки экрана смерти (UI Framework Game.log)
        self.death_screen_open = False
        self.last_death_screen_time = 0
        self.last_death_screen_close_time = 0

    def feed_game_log_line(self, line):
        line = line.strip()
        if not line:
            return

        with self.lock:
            # 0. Детекция выбора режима / лобби в главном меню
            if "DeepLinkingTarget" in line or "SelectDeepLinkingTarget" in line:
                if "QuickplayLobby" in line:
                    self.current_game_mode = "Quickplay"
                elif "ShootingRangeLobby" in line:
                    self.current_game_mode = "ShootingRange"
                elif "BountyClashLobby" in line:
                    self.current_game_mode = "BountyClash"
                elif "BountyHuntLobby" in line:
                    self.current_game_mode = "BountyHunt"

            if "[LocalMission|ShootingRange]" in line:
                self.current_game_mode = "ShootingRange"

            # 1. Проверка входа в матч / смены карты
            # Пример: <18:11:38> [CryNetwork]  391740 Game_Context ============================ PrepareLevel 'levels/cemetery' (client) ============================
            if "PrepareLevel" in line:
                match = re.search(r"PrepareLevel\s+'([^']+)'", line)
                if match:
                    level_name = match.group(1).lower()
                    if level_name in EXCLUDED_MAPS or "shooting" in level_name or "tutorial" in level_name:
                        # Если вернулись в 'menu', зашли на стрельбище или обучение
                        if self.session_active:
                            self._end_session("returned_to_menu")
                    else:
                        # В боевой статистике KDA участвуют строго Охота за наградой и Схватка за награду.
                        # Спасённая душа (Quickplay) и стрельбище полностью исключаются!
                        if self.current_game_mode in ["Quickplay", "ShootingRange"]:
                            if self.session_active:
                                self._end_session("casual_mode_detected")
                            return

                        # Боевая карта Охоты или Схватки
                        if not self.session_active or self.current_map != level_name:
                            self._start_session(level_name)
                return

            # 2. Проверка выхода из матча (Disconnect / Shutdown)
            if "Client_OnDisconnect" in line:
                if self.session_active:
                    self._end_session("disconnect")
                return

            # 3. Сигнал экрана смерти охотника: Starting/Stopping UI state machine 'death/StateMachine'
            # ВНИМАНИЕ: Сам по себе экран смерти НЕ является доказательством гибели игрока!
            # Он повторно открывается при наблюдении за напарником (в спектаторе) и при выходе.
            # Настоящая смерть локального игрока всегда сопровождается событием NVIDIA Highlights.
            if "Starting UI state machine 'death/StateMachine'" in line:
                now = time.monotonic()
                self.death_screen_open = True
                self.last_death_screen_time = now
                return

            if "Stopping UI state machine 'death/StateMachine'" in line:
                self.death_screen_open = False
                self.last_death_screen_close_time = time.monotonic()
                return

    def feed_nvidia_line(self, line):
        line = line.strip()
        if not line:
            return

        # Сигнал видеодрайвера: Posted Request HLSetVideo[...]
        # Является ЕДИНСТВЕННЫМ первичным триггером боевых действий локального охотника.
        if "Posted Request HLSetVideo" in line:
            with self.lock:
                if not self.session_active:
                    # Вне матча хайлайты не засчитываются
                    return

                now = time.monotonic()

                # Проверяем корреляцию: совпал ли сигнал NVIDIA с открытием экрана смерти в окне 2.5 сек
                is_death_correlated = self.death_screen_open or (now - self.last_death_screen_time) <= 2.5

                if is_death_correlated:
                    # Событие: СМЕРТЬ ЛОКАЛЬНОГО ОХОТНИКА
                    # Защита от дублирования сигналов одного и того же события гибели
                    if (now - self.last_death_time) <= 1.5 and self.last_death_time > 0:
                        return

                    self.deaths += 1
                    self.last_death_time = now
                    is_trade = False

                    # Проверка на размен (Trade): убийство врага было совершено <= 1.5 сек назад
                    if (now - self.last_kill_time) <= 1.5 and self.last_kill_time > 0:
                        self.trades += 1
                        is_trade = True

                    if self.on_event:
                        try:
                            ev_type = "trade" if is_trade else "death"
                            self.on_event(ev_type, 1, self._get_match_stats())
                        except Exception:
                            pass
                else:
                    # Событие: УБИЙСТВО ВРАГА
                    # Защита от эха драйвера после смерти
                    if (now - self.last_death_time) <= 0.45 and self.last_death_time > 0:
                        return

                    self.kills += 1
                    self.last_kill_time = now
                    is_trade = False

                    # Проверка на размен (Trade): охотник погиб <= 1.5 сек назад
                    if (now - self.last_death_time) <= 1.5 and self.last_death_time > 0:
                        self.trades += 1
                        is_trade = True

                    if self.on_event:
                        try:
                            ev_type = "trade" if is_trade else "kill"
                            self.on_event(ev_type, 1, self._get_match_stats())
                        except Exception:
                            pass

    def _start_session(self, map_name):
        self.session_active = True
        self.current_map = map_name
        self.match_id = f"hunt_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.match_start_time = time.time()
        self.kills = 0
        self.deaths = 0
        self.trades = 0
        self.last_kill_time = 0
        self.last_death_time = 0
        self.death_screen_open = False
        self.last_death_screen_time = 0
        self.last_death_screen_close_time = 0

        if self.on_session_start:
            try:
                self.on_session_start(self.current_map, self.match_id)
            except Exception:
                pass

    def _end_session(self, reason):
        if not self.session_active:
            return

        stats = self._get_match_stats()
        stats["reason"] = reason
        stats["duration_seconds"] = int(time.time() - self.match_start_time)

        self.session_active = False
        self.current_map = None

        if self.on_session_end:
            try:
                self.on_session_end(stats)
            except Exception:
                pass

    def _get_match_stats(self):
        return {
            "match_id": self.match_id,
            "map": self.current_map,
            "game_mode": self.current_game_mode,
            "kills": self.kills,
            "deaths": self.deaths,
            "trades": self.trades,
            "duration": int(time.time() - self.match_start_time) if self.match_start_time else 0
        }
