import time
import re
import uuid
import threading
import logger

log = logger.get_logger("Combat")

EXCLUDED_MAPS = [
    'menu',
    'levels/shooting',
    'levels/tutorial_scripted',
    'tutorial'
]


class SystemClock:
    """Системные часы по умолчанию (реальное время)."""
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()


class SystemScheduler:
    """Планировщик по умолчанию на базе threading.Timer."""
    def schedule(self, delay: float, callback, args=None):
        timer = threading.Timer(delay, callback, args=args or [])
        timer.daemon = True
        timer.start()
        return timer


class TestTimerHandle:
    """Управляемый дескриптор таймера для офлайн-тестов."""
    def __init__(self, scheduler, item):
        self.scheduler = scheduler
        self.item = item
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self.item in self.scheduler.scheduled_items:
            self.scheduler.scheduled_items.remove(self.item)


class TestClock:
    """Детерминированные часы и планировщик для офлайн-тестов без sleep и потоков."""
    def __init__(self, initial_monotonic=1000.0, initial_time=1700000000.0):
        self._monotonic = float(initial_monotonic)
        self._time = float(initial_time)
        self.scheduled_items = []

    def monotonic(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self._time

    def schedule(self, delay: float, callback, args=None):
        fire_at = self._monotonic + delay
        item = {
            "fire_at": fire_at,
            "callback": callback,
            "args": args or [],
            "handle": None
        }
        handle = TestTimerHandle(self, item)
        item["handle"] = handle
        self.scheduled_items.append(item)
        self.scheduled_items.sort(key=lambda x: x["fire_at"])
        return handle

    def advance(self, seconds: float):
        """Продвигает виртуальное время и запускает созревшие таймеры."""
        target_monotonic = self._monotonic + seconds
        self._time += seconds

        while self.scheduled_items:
            self.scheduled_items.sort(key=lambda x: x["fire_at"])
            next_item = self.scheduled_items[0]
            if next_item["fire_at"] <= target_monotonic:
                self.scheduled_items.pop(0)
                if not next_item["handle"].cancelled:
                    self._monotonic = next_item["fire_at"]
                    next_item["callback"](*next_item["args"])
            else:
                break
        self._monotonic = target_monotonic


class CombatStateMachine:
    """
    Конечный автомат сессий и боевых событий Hunt: Showdown 1896.
    Сверяет сигналы Game.log и NVIDIA GfeSDK в реальном времени.
    Обеспечивает точный учет фрагов, смертей и взаимных разменов (Trades).
    Использует асинхронный 2.7-секундный буфер корреляции хайлайтов NVIDIA 
    для компенсации задержки UI CryEngine при анимации гибели.
    """
    def __init__(self, on_session_start=None, on_session_end=None, on_event=None, clock=None, scheduler=None):
        self.on_session_start = on_session_start
        self.on_session_end = on_session_end
        self.on_event = on_event
        self.clock = clock or SystemClock()
        self.scheduler = scheduler or (self.clock if hasattr(self.clock, 'schedule') else SystemScheduler())

        self.lock = threading.RLock()
        self.session_active = False
        self.current_map = None
        self.current_game_mode = "BountyHunt"  # 'BountyHunt', 'BountyClash', 'Quickplay', 'ShootingRange'
        self.match_id = None
        self.match_start_time = 0
        self.kills = 0
        self.deaths = 0
        self.trades = 0

        # Временные метки для детекции размеров и фильтрации эха
        self.last_death_time = 0
        self.last_kill_time = 0

        # Метки экрана смерти (UI Framework Game.log)
        self.death_screen_open = False
        self.death_screen_paired = False  # Флаг: текущий экран смерти уже спарен с хайлайтом NVIDIA
        self.last_death_screen_time = 0
        self.last_death_screen_close_time = 0

        # Буфер отложенных хайлайтов NVIDIA для точной корреляции
        # Список словарей: [{"id": str, "time": float, "timer": threading.Timer}]
        self.pending_highlights = []

        # Константы временных окон боевых событий
        self.TRADE_WINDOW = 1.0  # 1.0 сек дельта между киллом и смертью (полное серверное окно размена Hunt: Showdown)
        self.DEATH_DEBOUNCE = 2.0  # 2 сек защита от дребезга экранов UI
        self.KILL_CONFIRMATION_TIMEOUT = 1.2  # 1.2 сек ожидание экрана смерти перед подтверждением фрага
        self.MAX_MATCH_DURATION = 47 * 60  # 47 минут — максимальная длительность матча в Hunt
        self.RECONNECT_DEATH_HIGHLIGHT_WINDOW = 3.0  # Окно подавления повторного хайлайта при входе на экран смерти

        # Сетевые идентификаторы и состояние реконнекта
        self.current_server_ip = None
        self.current_mission_log_id = None
        self.last_match_server_ip = None
        self.last_match_log_id = None
        self.returned_to_menu = True
        self.awaiting_reconnect = False
        self.is_reconnect_fastforward = False
        self.was_dead_before_dc = False
        self.expecting_reconnect_death_ui = False
        self.reconnect_reload_highlight_consumed = False
        self.reconnect_death_ui_opened_at = 0
        self.reconnect_timer = None
        self.is_warming_up = False
        self._deferred_warmup_end_stats = None

        # Якорь загрузки уровня и компенсирующий откат запоздалого экрана смерти
        self.is_level_loading = False
        self.last_loading_ended_at = 0.0
        self.last_timeout_kill = None

    def _log_info(self, msg):
        if not self.is_warming_up:
            log.info(msg)

    def _cancel_reconnect_timer(self):
        if self.reconnect_timer:
            try:
                self.reconnect_timer.cancel()
            except Exception:
                pass
            self.reconnect_timer = None

    def _schedule_reconnect_timeout(self):
        self._cancel_reconnect_timer()
        elapsed = max(0.0, self.clock.time() - self.match_start_time) if self.match_start_time else 0.0
        remaining = max(1.0, self.MAX_MATCH_DURATION - elapsed)
        expected_id = self.match_id
        self.reconnect_timer = self.scheduler.schedule(remaining, self._on_reconnect_timeout, args=[expected_id])

    def _on_reconnect_timeout(self, expected_match_id):
        with self.lock:
            if self.awaiting_reconnect and self.match_id == expected_match_id:
                log.info(f"[DECISION] Истекли 47 минут ожидания реконнекта (ID={expected_match_id}). Закрытие матча.")
                self._end_session("reconnect_timeout_47m")

    def restore_pending_reconnect(self, saved):
        """Переводит сохранённый после сбоя матч в режим ожидания переподключения."""
        with self.lock:
            self.session_active = False
            self.awaiting_reconnect = True
            self.returned_to_menu = False
            self.match_id = saved.get("match_id")
            self.current_map = saved.get("map")
            self.current_game_mode = saved.get("game_mode") or "BountyHunt"
            self.kills = int(saved.get("kills", 0))
            self.deaths = int(saved.get("deaths", 0))
            self.trades = int(saved.get("trades", 0))
            self.match_start_time = float(saved.get("start_time", self.clock.time()))
            self.last_match_server_ip = saved.get("server_ip")
            self.last_match_log_id = saved.get("log_id")
            self.was_dead_before_dc = bool(saved.get("was_dead", False))
            self.death_screen_open = False
            self.death_screen_paired = False
            log.info(
                f"[RECOVERY] Ожидание реконнекта после сбоя: ID={self.match_id}, "
                f"K={self.kills}, D={self.deaths}, IP={self.last_match_server_ip}, was_dead={self.was_dead_before_dc}"
            )
            self._schedule_reconnect_timeout()

    def warmup_from_game_log(self, log_path):
        """
        Восстанавливает актуальное состояние запущенного процесса Hunt из Game.log.
        Позволяет подхватить активный матч при позднем запуске трекера и сшить его с восстановленным.
        """
        if not log_path:
            return
        with self.lock:
            was_awaiting = self.awaiting_reconnect
            prev_match_id = self.match_id
            self._deferred_warmup_end_stats = None
            self.is_warming_up = True
            try:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        self.feed_game_log_line(line)
            except Exception:
                pass
            finally:
                self.is_warming_up = False

            deferred_end = self._deferred_warmup_end_stats
            self._deferred_warmup_end_stats = None

            if self.session_active:
                self._cancel_reconnect_timer()
                if was_awaiting and self.match_id == prev_match_id:
                    log.info(
                        f"[WARMUP] Сшивание прерванного матча с активной игрой: "
                        f"ID={self.match_id}, Карта='{self.current_map}', K={self.kills}, D={self.deaths}"
                    )
                    if self.on_event:
                        try:
                            self.on_event("reconnect", 0, self._get_match_stats())
                        except Exception:
                            pass
                else:
                    log.info(
                        f"[WARMUP] Горячий подхват активного матча: "
                        f"ID={self.match_id}, Карта='{self.current_map}', IP={self.last_match_server_ip}"
                    )
                    if self.on_session_start:
                        try:
                            self.on_session_start(self.current_map, self.match_id)
                        except Exception:
                            pass
            elif deferred_end and was_awaiting and deferred_end.get("match_id") == prev_match_id:
                log.info(f"[WARMUP] Прерванный матч уже завершен в текущем Game.log ({deferred_end.get('reason')}). Фиксация...")
                if self.on_session_end:
                    try:
                        self.on_session_end(deferred_end)
                    except Exception:
                        pass

    def feed_game_log_line(self, line):
        line = line.strip()
        if not line:
            return

        with self.lock:
            # 0. Детекция выбора режима / лобби в главном меню
            prev_mode = self.current_game_mode
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

            if self.current_game_mode != prev_mode:
                self._log_info(f"[RAW:Game.log] Режим меню: {prev_mode} -> {self.current_game_mode}")

            # 0.05. Расширенные диагностические логи UI и FSM переподключения
            if "CLobbyFsm::Update()" in line and "eLFSMI_" in line:
                self._log_info(f"[RAW:Game.log] FSM: {line}")
                if "eLFSMI_ReconnectFastforward" in line:
                    self.is_reconnect_fastforward = True

            if "to (eLS_Loading)" in line or "Starting UI state machine 'loading/StateMachine'" in line:
                self.is_level_loading = True

            if "to (eLS_Game)" in line or "Stopping UI state machine 'loading/StateMachine'" in line:
                self.is_level_loading = False
                self.last_loading_ended_at = self.clock.monotonic()

            if "UI state machine" in line and any(
                tag in line for tag in (
                    "reconnect/", "loading/", "lobby/", "menu/", "hud/"
                )
            ):
                self._log_info(f"[RAW:Game.log] UI: {line}")

            # 0.1. Фиксация подключения к игровому серверу (IP:порт)
            if "Connection requested to:" in line and "<local>" not in line:
                match_ip = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)", line)
                if match_ip:
                    self.current_server_ip = match_ip.group(1)
                    self._log_info(f"[RAW:Game.log] Подключение к игровому серверу: {self.current_server_ip}")

            # 0.2. Фиксация LogId миссии
            if "starting mission with LogId:" in line:
                match_logid = re.search(r"starting mission with LogId:\s*([0-9a-zA-Z_\-]+)", line)
                if match_logid:
                    self.current_mission_log_id = match_logid.group(1)
                    self._log_info(f"[RAW:Game.log] Инициализация миссии LogId: {self.current_mission_log_id}")

            # 0.3. Завершение матча по послематчевым экранам или реальному выходу в лобби
            is_lobby_or_summary = (
                "post_match_summary/StateMachine" in line
                or "teamdetails/StateMachine" in line
                or "Starting UI state machine 'lobby/StateMachine'" in line
            )
            if is_lobby_or_summary:
                self._log_info(f"[RAW:Game.log] Экран лобби/итогов: {line}")
                self.returned_to_menu = True
                self.is_reconnect_fastforward = False
                if self.session_active or self.awaiting_reconnect:
                    reason = "reconnect_declined" if self.awaiting_reconnect else "match_summary"
                    self._end_session(reason)
                self.last_match_server_ip = None
                self.last_match_log_id = None
                self.was_dead_before_dc = False
                return

            # 1. Проверка входа в матч / смены карты
            if "PrepareLevel" in line:
                match = re.search(r"PrepareLevel\s+'([^']+)'", line)
                if match:
                    level_name = match.group(1).lower()
                    self._log_info(f"[RAW:Game.log] PrepareLevel: '{level_name}', mode='{self.current_game_mode}'")
                    if level_name in EXCLUDED_MAPS or "shooting" in level_name or "tutorial" in level_name:
                        self.is_reconnect_fastforward = False
                        if "shooting" in level_name or "tutorial" in level_name:
                            self.returned_to_menu = True
                            if self.session_active or self.awaiting_reconnect:
                                self._end_session("returned_to_menu")
                            self.last_match_server_ip = None
                            self.last_match_log_id = None
                            self.was_dead_before_dc = False
                        elif level_name == "menu":
                            # Загрузка главного меню при старте игры не завершает матч (ждём ReconnectFlow или lobby/post_match_summary)
                            if self.session_active:
                                self._pause_for_reconnect("menu_prepare_after_dc")
                    else:
                        if self.current_game_mode in ["Quickplay", "ShootingRange"]:
                            if self.session_active or self.awaiting_reconnect:
                                self._end_session("casual_mode_detected")
                            return

                        # Проверка на бесшовный реконнект
                        is_same_server = (
                            self.current_server_ip is not None
                            and self.last_match_server_ip is not None
                            and self.current_server_ip == self.last_match_server_ip
                        )
                        is_diff_server = (
                            self.current_server_ip is not None
                            and self.last_match_server_ip is not None
                            and self.current_server_ip != self.last_match_server_ip
                        )
                        is_same_log_id = (
                            self.current_mission_log_id is not None
                            and self.last_match_log_id is not None
                            and self.current_mission_log_id == self.last_match_log_id
                        )

                        can_reconnect = (
                            (self.awaiting_reconnect or not self.returned_to_menu)
                            and not is_diff_server
                            and (is_same_server or is_same_log_id or self.is_reconnect_fastforward)
                        )
                        if can_reconnect and self.match_id:
                            self._resume_reconnected_match(level_name)
                            return

                        if self.session_active or self.awaiting_reconnect:
                            self._end_session("new_match_started")

                        self._start_session(level_name)
                return

            # 2. Проверка выхода из матча (Disconnect / Shutdown)
            if "Client_OnDisconnect" in line:
                # Игнорируем уничтожение временного локального нуба при переходе в матч
                if "Nub destroyed" in line:
                    return

                self._log_info(f"[RAW:Game.log] Client_OnDisconnect: {line}")
                # Штатный выход игрока или завершение сессии сервером
                if "UserRequested" in line or "MissionRequested" in line or "GameSessionEnded" in line:
                    self.returned_to_menu = True
                    self.was_dead_before_dc = False
                    self.is_reconnect_fastforward = False
                    if self.session_active or self.awaiting_reconnect:
                        self._end_session("disconnect_clean")
                    self.last_match_server_ip = None
                    self.last_match_log_id = None
                    return

                # Аварийный выход / Alt+F4 / обрыв связи: фиксируем состояние смерти ДО сноса UI игрой
                if self.death_screen_open:
                    self.was_dead_before_dc = True
                if self.session_active:
                    self._pause_for_reconnect("disconnect_unclean")
                return

            # 3. Сигнал экрана смерти охотника: Starting/Stopping UI state machine 'death/StateMachine'
            if "Starting UI state machine 'death/StateMachine'" in line:
                now = self.clock.monotonic()
                if self.expecting_reconnect_death_ui:
                    self.expecting_reconnect_death_ui = False
                    self.was_dead_before_dc = False
                    self.death_screen_open = True
                    self.death_screen_paired = True
                    self.last_death_screen_time = now
                    self.reconnect_death_ui_opened_at = now
                    self._log_info("[DECISION] РЕКОННЕКТ МЁРТВЫМ: Восстановлен экран смерти (дубль гибели подавлен)")
                    return

                self._log_info(f"[RAW:Game.log] Открытие death/StateMachine (open={self.death_screen_open}, paired={self.death_screen_paired})")
                # Защита от повторного открытия при быстром переключении камер (Kill View / Spectate)
                if self.death_screen_open and (now - self.last_death_screen_time) <= self.DEATH_DEBOUNCE:
                    self._log_info(f"[DECISION] Подавлен дребезг экрана смерти (дельта {now - self.last_death_screen_time:.3f}с <= {self.DEATH_DEBOUNCE}с)")
                    return

                self.death_screen_open = True
                self.death_screen_paired = False
                self.last_death_screen_time = now

                # Двусторонняя корреляция (NVIDIA хайлайт пришел чуть раньше Game.log)
                # Ищем в буфере самый свежий хайлайт за последние 2.0 сек
                matched = None
                for p in reversed(self.pending_highlights):
                    if (now - p["time"]) <= 2.0:
                        matched = p
                        break

                if matched:
                    delta_sec = now - matched["time"]
                    self._log_info(f"[DECISION] ПОДТВЕРЖДЕНА СМЕРТЬ: Экран смерти спарен с хайлайтом NVIDIA из буфера (дельта {delta_sec:.3f}с)")
                    self.pending_highlights.remove(matched)
                    if matched.get("timer"):
                        try:
                            matched["timer"].cancel()
                        except Exception:
                            pass
                    self.death_screen_paired = True
                    self.last_timeout_kill = None
                    if not self.is_warming_up:
                        self._record_death(matched["time"])
                else:
                    # Буфер пуст. Проверяем осторожный компенсирующий откат ложного килла по таймауту
                    if (
                        self.last_timeout_kill
                        and len(self.pending_highlights) == 0
                        and (now - self.last_timeout_kill["time"]) <= 0.75
                    ):
                        timeout_delta = now - self.last_timeout_kill["time"]
                        log.warning(
                            f"[DECISION:ROLLBACK] ОТКАТ ЛОЖНОГО КИЛЛА: Экран смерти открылся через {timeout_delta:.3f}с "
                            f"после подтверждения по таймауту (гонка UI при загрузке)"
                        )
                        self.kills = max(0, self.kills - 1)
                        if self.last_timeout_kill.get("is_trade"):
                            self.trades = max(0, self.trades - 1)
                        self.last_kill_time = 0
                        self.last_timeout_kill = None
                        self.death_screen_paired = True
                        if not self.is_warming_up:
                            self._record_death(now)
                return

            if "Stopping UI state machine 'death/StateMachine'" in line:
                self._log_info("[RAW:Game.log] Закрытие death/StateMachine (воскрешение/сброс)")
                self.death_screen_open = False
                self.death_screen_paired = False  # Сброс при воскрешении (готов к следующей смерти)
                self.last_death_screen_close_time = self.clock.monotonic()
                if self.session_active and not self.awaiting_reconnect:
                    # Реальное воскрешение в бою (а не закрытие UI при Alt+F4)
                    self.was_dead_before_dc = False
                    self.expecting_reconnect_death_ui = False
                    self.reconnect_death_ui_opened_at = 0
                    if self.on_event and not self.is_warming_up:
                        try:
                            self.on_event("revive", 0, self._get_match_stats())
                        except Exception:
                            pass
                return

    def feed_nvidia_line(self, line):
        line = line.strip()
        if not line:
            return

        # Сигнал видеодрайвера: Posted Request HLSetVideo[...]
        if "Posted Request HLSetVideo" in line:
            with self.lock:
                log.info(f"[RAW:NVIDIA] Сигнал драйвера: {line}")
                if not self.session_active:
                    log.info(f"[DECISION] ИГНОРИРОВАНИЕ ХАЙЛАЙТА: Боевой матч не активен (в лобби или меню)")
                    return

                now = self.clock.monotonic()

                # Подавление повторного хайлайта NVIDIA при загрузке на экран смерти после реконнекта
                if self.expecting_reconnect_death_ui and not self.reconnect_reload_highlight_consumed:
                    self.reconnect_reload_highlight_consumed = True
                    log.info("[DECISION] РЕКОННЕКТ МЁРТВЫМ: Подавлен повторный хайлайт загрузки экрана смерти")
                    return
                if (
                    self.reconnect_death_ui_opened_at > 0
                    and (now - self.reconnect_death_ui_opened_at) <= self.RECONNECT_DEATH_HIGHLIGHT_WINDOW
                    and not self.reconnect_reload_highlight_consumed
                ):
                    self.reconnect_reload_highlight_consumed = True
                    log.info("[DECISION] РЕКОННЕКТ МЁРТВЫМ: Подавлен запоздавший хайлайт загрузки экрана смерти")
                    return

                # Двусторонняя корреляция и обработка разменов
                # Если экран смерти УЖЕ открыт:
                if self.death_screen_open:
                    # 1. Если экран смерти открылся недавно (до 2.0 сек) и еще не был спарен — это гибель охотника
                    if not self.death_screen_paired and (now - self.last_death_screen_time) <= 2.0:
                        delta_sec = now - self.last_death_screen_time
                        log.info(f"[DECISION] ПОДТВЕРЖДЕНА СМЕРТЬ: Хайлайт спарен с открытым экраном смерти (дельта {delta_sec:.3f}с)")
                        self.death_screen_paired = True
                        self._record_death(now)
                        return
                    else:
                        # 2. Если экран смерти уже спарен (гибель уже зафиксирована) — 
                        # это либо фраг при взаимном размене, либо посмертное убийство (ловушка / яд / огонь).
                        # Так как драйвер NVIDIA шлет сигналы исключительно на личные события игрока,
                        # начисляем убийство без блокировки
                        death_delta = now - self.last_death_time if self.last_death_time > 0 else 0
                        log.info(f"[DECISION] РАЗМЕН / ПОСМЕРТНЫЙ КИЛЛ: Хайлайт получен при уже спаренной смерти (дельта от гибели {death_delta:.3f}с)")
                        self._record_kill(now)
                        return

                # Если экран закрылся, но гибель произошла только что в пределах окна размена (до 1.0 сек),
                # а хайлайт нашего килла пришел с задержкой:
                if self.last_death_time > 0 and (now - self.last_death_time) <= self.TRADE_WINDOW:
                    trade_delta = now - self.last_death_time
                    log.info(f"[DECISION] РАЗМЕН: Хайлайт в окне размена после закрытия экрана смерти (дельта {trade_delta:.3f}с <= {self.TRADE_WINDOW}с)")
                    self._record_kill(now)
                    return

                # Иначе: кандидат на фраг (КИЛЛ в живом состоянии)
                # Ожидаем окно подтверждения (1.2 сек)
                # Динамическое окно подтверждения: 3.0 с если идет загрузка или прошло <= 10.0 с после конца загрузки уровня
                is_recent_loading = (
                    self.is_level_loading
                    or (self.last_loading_ended_at > 0 and (now - self.last_loading_ended_at) <= 10.0)
                )
                timeout_window = 3.0 if is_recent_loading else self.KILL_CONFIRMATION_TIMEOUT

                highlight_id = uuid.uuid4().hex
                suffix = " (после загрузки уровня)" if is_recent_loading else ""
                log.info(f"[DECISION] КАНДИДАТ НА КИЛЛ: Хайлайт {highlight_id[:8]} поставлен в буфер подтверждения на {timeout_window}с{suffix}")
                timer = self.scheduler.schedule(timeout_window, self._on_highlight_timeout, args=[highlight_id])
                self.pending_highlights.append({
                    "id": highlight_id,
                    "time": now,
                    "timer": timer
                })

    def _on_highlight_timeout(self, highlight_id):
        with self.lock:
            if not (self.session_active or self.awaiting_reconnect):
                return

            # Находим нужный элемент в буфере
            target = None
            for p in self.pending_highlights:
                if p["id"] == highlight_id:
                    target = p
                    break

            if not target:
                return

            self.pending_highlights.remove(target)

            now = self.clock.monotonic()
            # Проверяем, не открылся ли неспаренный экран смерти
            if not self.death_screen_paired and (self.death_screen_open or (now - self.last_death_screen_time) <= 2.0):
                log.info(f"[DECISION] ПОДТВЕРЖДЕНА СМЕРТЬ ПО ТАЙМАУТУ: Хайлайт {highlight_id[:8]} спарен с открывшимся экраном смерти")
                self.death_screen_paired = True
                self._record_death(target["time"])
            else:
                log.info(f"[DECISION] ПОДТВЕРЖДЕН КИЛЛ ПО ТАЙМАУТУ: Хайлайт {highlight_id[:8]} подтвержден как убийство охотника (экранов смерти не появилось)")
                is_trade = self._record_kill(target["time"])
                self.last_timeout_kill = {
                    "time": self.clock.monotonic(),
                    "is_trade": is_trade,
                    "highlight_id": highlight_id
                }

    def _record_kill(self, timestamp):
        self.kills += 1
        is_trade = False

        # Точная проверка взаимного размена по временным меткам событий
        if self.last_death_time > 0 and abs(timestamp - self.last_death_time) <= self.TRADE_WINDOW:
            self.trades += 1
            is_trade = True

        self.last_kill_time = timestamp
        log.info(f"[EVENT:KILL] Зафиксирован килл #{self.kills} (is_trade={is_trade}, trades_total={self.trades})")

        if self.on_event:
            try:
                ev_type = "trade" if is_trade else "kill"
                self.on_event(ev_type, 1, self._get_match_stats())
            except Exception:
                pass
        return is_trade

    def _record_death(self, timestamp):
        # Защита от повторной фиксации одной и той же смерти
        if self.last_death_time > 0 and abs(timestamp - self.last_death_time) <= self.DEATH_DEBOUNCE:
            log.info(f"[DECISION] Подавление повторной смерти (дельта {abs(timestamp - self.last_death_time):.3f}с <= {self.DEATH_DEBOUNCE}с)")
            return

        self.deaths += 1
        is_trade = False

        # Точная проверка взаимного размена по временным меткам событий
        if self.last_kill_time > 0 and abs(timestamp - self.last_kill_time) <= self.TRADE_WINDOW:
            self.trades += 1
            is_trade = True

        self.last_timeout_kill = None
        self.last_death_time = timestamp
        log.info(f"[EVENT:DEATH] Зафиксирована гибель #{self.deaths} (is_trade={is_trade}, trades_total={self.trades})")

        if self.on_event:
            try:
                ev_type = "trade" if is_trade else "death"
                self.on_event(ev_type, 1, self._get_match_stats())
            except Exception:
                pass

    def _clear_pending_highlights(self):
        for p in self.pending_highlights:
            if p.get("timer"):
                try:
                    p["timer"].cancel()
                except Exception:
                    pass
        self.pending_highlights.clear()
        self.last_timeout_kill = None

    def _pause_for_reconnect(self, reason):
        """Ставит матч на паузу при аварийном выходе / Alt+F4 и запускает 47-минутный таймер."""
        self.session_active = False
        self.awaiting_reconnect = True
        self._schedule_reconnect_timeout()
        self._log_info(
            f"[MATCH:PAUSE] Пауза матча для реконнекта ({reason}): "
            f"ID={self.match_id}, K={self.kills}, D={self.deaths}, was_dead={self.was_dead_before_dc}"
        )
        if self.on_event and not self.is_warming_up:
            try:
                self.on_event("reconnect_pause", 0, self._get_match_stats())
            except Exception:
                pass

    def _resume_reconnected_match(self, level_name):
        """Бесшовно продолжает прерванный матч при возврате на тот же сервер."""
        self._cancel_reconnect_timer()
        self.session_active = True
        self.awaiting_reconnect = False
        self.returned_to_menu = False
        self.current_map = level_name
        self.is_reconnect_fastforward = False
        if not self.last_match_server_ip and self.current_server_ip:
            self.last_match_server_ip = self.current_server_ip
        if not self.last_match_log_id and self.current_mission_log_id:
            self.last_match_log_id = self.current_mission_log_id

        self.expecting_reconnect_death_ui = bool(self.was_dead_before_dc)
        self.reconnect_reload_highlight_consumed = False
        self.reconnect_death_ui_opened_at = 0

        self._log_info(
            f"[DECISION] БЕСШОВНЫЙ РЕКОННЕКТ к текущему матчу "
            f"(ID={self.match_id}, IP={self.last_match_server_ip}, LogId={self.last_match_log_id}, was_dead={self.was_dead_before_dc})"
        )
        if self.on_event and not self.is_warming_up:
            try:
                self.on_event("reconnect", 0, self._get_match_stats())
            except Exception:
                pass

    def _start_session(self, map_name):
        self._cancel_reconnect_timer()
        self._clear_pending_highlights()
        self.session_active = True
        self.awaiting_reconnect = False
        self.returned_to_menu = False
        self.current_map = map_name
        self.match_id = f"hunt_{int(self.clock.time())}_{uuid.uuid4().hex[:6]}"
        self.match_start_time = self.clock.time()
        self.kills = 0
        self.deaths = 0
        self.trades = 0
        self.last_kill_time = 0
        self.last_death_time = 0
        self.death_screen_open = False
        self.death_screen_paired = False
        self.last_death_screen_time = 0
        self.last_death_screen_close_time = 0
        self.was_dead_before_dc = False
        self.expecting_reconnect_death_ui = False
        self.reconnect_reload_highlight_consumed = False
        self.reconnect_death_ui_opened_at = 0
        self.is_reconnect_fastforward = False

        # Фиксируем параметры сервера для бесшовного реконнекта
        self.last_match_server_ip = self.current_server_ip
        self.last_match_log_id = self.current_mission_log_id

        self._log_info(f"[MATCH:START] Новый матч: ID={self.match_id}, Карта='{self.current_map}', Режим='{self.current_game_mode}', IP={self.last_match_server_ip}, LogId={self.last_match_log_id}")

        if self.on_session_start and not self.is_warming_up:
            try:
                self.on_session_start(self.current_map, self.match_id)
            except Exception:
                pass

    def _end_session(self, reason):
        with self.lock:
            if not (self.session_active or self.awaiting_reconnect):
                return

            self._cancel_reconnect_timer()

            # Если при выходе остался висящий хайлайт в буфере:
            now = self.clock.monotonic()
            for p in list(self.pending_highlights):
                if p.get("timer"):
                    try:
                        p["timer"].cancel()
                    except Exception:
                        pass
                if not self.death_screen_paired and (self.death_screen_open or (now - self.last_death_screen_time) <= 2.0):
                    self.death_screen_paired = True
                    self._record_death(p["time"])
                else:
                    self._record_kill(p["time"])
            self.pending_highlights.clear()

            stats = self._get_match_stats()
            stats["reason"] = reason
            raw_duration = int(self.clock.time() - self.match_start_time) if self.match_start_time else 0
            stats["duration_seconds"] = min(self.MAX_MATCH_DURATION, max(0, raw_duration))

            self._log_info(f"[MATCH:END] Завершение матча: ID={stats['match_id']}, K={stats['kills']}, D={stats['deaths']}, T={stats['trades']}, Длительность={stats['duration_seconds']}с, Причина='{reason}'")

            self.session_active = False
            self.awaiting_reconnect = False
            self.current_map = None
            self.match_id = None
            self.was_dead_before_dc = False
            self.expecting_reconnect_death_ui = False
            self.reconnect_reload_highlight_consumed = False
            self.reconnect_death_ui_opened_at = 0

            if self.is_warming_up:
                self._deferred_warmup_end_stats = stats
                return

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
            "server_ip": self.last_match_server_ip,
            "log_id": self.last_match_log_id,
            "was_dead": bool(self.death_screen_open or self.was_dead_before_dc),
            "kills": self.kills,
            "deaths": self.deaths,
            "trades": self.trades,
            "duration": min(self.MAX_MATCH_DURATION, max(0, int(self.clock.time() - self.match_start_time))) if self.match_start_time else 0
        }
