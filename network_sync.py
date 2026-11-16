import os
import time
import json
import uuid
import queue
import threading
import urllib.request
import urllib.error
import config_manager
import logger

log = logger.get_logger("Network")

MAX_QUEUE_AGE = 14400  # 4 часа в секундах
BACKOFF_STAGES = [3, 6, 12, 30]
MAX_ATTEMPTS = 20          # попыток на один матч, после чего он ждёт ручного повтора
MAX_RETRY_AFTER = 900      # верхний предел паузы по Retry-After (15 минут)
DEFAULT_RETRY_AFTER = 60   # пауза при 429 без заголовка Retry-After


class NetworkSync:
    """
    Модуль сетевой синхронизации с сайтом KD Metrika.
    Обеспечивает гарантированную доставку импульсов матчей
    через персистентную очередь на диске (%APPDATA%\\KDConnect\\pending_matches.json),
    единый воркер с экспоненциальным бэкоффом (3 -> 6 -> 12 -> 30с)
    и изоляцией сессий (session_id, manual_conflict, session_mismatch).
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.current_session_id = None
        self.session_lost = False
        self.manual_conflict = False
        self._waiting_session_logged = False
        self.running = True
        self.backoff_idx = 0
        self.wake_event = threading.Event()

        # Постоянные ошибки доставки (этап 16): 401/403 и прочие неустранимые 4xx
        self.token_error = None      # {"code": int, "message": str} — сервер отклонил токен
        self.next_delay = None       # принудительная пауза после 429 (Retry-After)

        # 1. Загрузка персистентной очереди с диска и отсечка записей старше 4 часов
        self.retry_queue = self._load_and_filter_initial_queue()

        # 2. Очередь для неблокирующих боевых импульсов (state pulses)
        self.state_queue = queue.Queue(maxsize=100)
        self.state_worker_thread = threading.Thread(target=self._state_worker_loop, daemon=True)
        self.state_worker_thread.start()

        # 3. Единый рабочий поток отправки очереди матчей
        self.worker_thread = threading.Thread(target=self._retry_worker_loop, daemon=True)
        self.worker_thread.start()

    def get_current_session_id(self):
        """Возвращает текущий активный session_id под блокировкой."""
        with self.lock:
            return self.current_session_id

    def set_current_session_id(self, session_id):
        """Фиксирует активный session_id для привязки новых матчей."""
        with self.lock:
            self.current_session_id = str(session_id).strip() if session_id else None
            # Если привязали сессию, сбрасываем флаг ожидания сессии и сбрасываем session_lost
            if self.current_session_id:
                self._waiting_session_logged = False
                self.session_lost = False
                self.wake_event.set()

    def consume_session_lost(self):
        """Возвращает флаг потери сессии и атомарно сбрасывает его."""
        with self.lock:
            lost = self.session_lost
            self.session_lost = False
            return lost

    def set_manual_conflict(self, conflict: bool):
        """Устанавливает или сбрасывает флаг конфликта ручной сессии."""
        with self.lock:
            self.manual_conflict = bool(conflict)
            if not self.manual_conflict:
                self.wake_event.set()

    def get_delivery_state(self):
        """Состояние доставки для интерфейса: отклонённый токен, застрявшие матчи, пауза."""
        with self.lock:
            failed = sum(1 for item in self.retry_queue if item.get("permanent_fail"))
            return {
                "token_error": bool(self.token_error),
                "token_error_code": (self.token_error or {}).get("code"),
                "token_error_message": (self.token_error or {}).get("message"),
                "failed_count": failed,
                "queued_count": len(self.retry_queue),
                "retry_after": int(self.next_delay) if self.next_delay else 0,
            }

    def retry_failed(self):
        """Ручной повтор: снимает отметки постоянных ошибок и снова ставит матчи в отправку."""
        with self.lock:
            for item in self.retry_queue:
                item.pop("permanent_fail", None)
                item.pop("fail_reason", None)
                item.pop("attempts", None)
            self.token_error = None
            self.next_delay = None
            self.backoff_idx = 0
            self._waiting_session_logged = False
            self._persist_queue()
        self.wake_event.set()
        log.info("[NET:RETRY_RESET] Ручной повтор: отметки постоянных ошибок сняты, отправка возобновлена")
        return self.get_delivery_state()

    def _load_and_filter_initial_queue(self):
        saved = config_manager.load_pending_matches()
        if not saved or not isinstance(saved, list):
            return []
        now = time.time()
        kept = []
        expired = []
        for item in saved:
            if not isinstance(item, dict):
                continue
            created = float(item.get("created_at") or now)
            if (now - created) > MAX_QUEUE_AGE:
                expired.append(item)
            else:
                kept.append(item)
        if expired:
            log.warning(f"[NET:EXPIRED] При старте отброшено {len(expired)} устаревших матчей (>4ч): {[x.get('event_id') for x in expired]}")
            config_manager.save_pending_matches(kept)
        if kept:
            log.info(f"[NET:QUEUE_RESTORE] Из pending_matches.json восстановлено {len(kept)} неотправленных матчей")
        return kept

    def _persist_queue(self):
        """Атомарно сохраняет текущую очередь на диск."""
        try:
            config_manager.save_pending_matches(self.retry_queue)
        except Exception as e:
            log.error(f"[NET:PERSIST_ERR] Не удалось сохранить pending_matches.json: {e}")

    def get_pending_matches(self, max_age_seconds=MAX_QUEUE_AGE):
        """Возвращает список накопленных матчей за последние 4 часа, отсекая устаревшие."""
        with self.lock:
            now = time.time()
            kept = []
            for item in self.retry_queue:
                created = float(item.get("created_at") or now)
                if (now - created) <= max_age_seconds:
                    kept.append(item)
            if len(kept) != len(self.retry_queue):
                self.retry_queue = kept
                self._persist_queue()
            return list(self.retry_queue)

    def clear_pending_matches(self):
        """Принудительно очищает очередь повторов в памяти и на диске."""
        with self.lock:
            cleared = len(self.retry_queue)
            self.retry_queue.clear()
            self._waiting_session_logged = False
            self.manual_conflict = False
            self.backoff_idx = 0
            self.token_error = None
            self.next_delay = None
            config_manager.clear_pending_matches_file()
        if cleared > 0:
            log.info(f"[NET:QUEUE_CLEAR] Буфер неотправленных матчей очищен (удалено {cleared} матчей)")

    def send_match_pulse(self, match_summary):
        """
        Гарантированно сохраняет завершенный матч в персистентную очередь на диск
        и инициирует доставку на сервер.
        """
        event_id = match_summary.get("match_id") or f"evt_{uuid.uuid4().hex[:12]}"
        created_at = match_summary.get("created_at")
        if not created_at:
            created_at = time.time()

        # Единый словарь режимов: BountyHuntLobby/BountyHunt -> bounty_hunt, BountyClash -> bounty_clash
        raw_mode = str(match_summary.get("game_mode") or "BountyHunt")
        if "clash" in raw_mode.lower():
            game_mode = "bounty_clash"
        elif "quick" in raw_mode.lower() or "soul" in raw_mode.lower():
            game_mode = "soul_survivor"
        else:
            game_mode = "bounty_hunt"

        payload = {
            "type": "match",
            "event_id": event_id,
            "match_id": match_summary.get("match_id"),
            "external_event_id": event_id,
            "session_id": self.current_session_id,  # UUID активной сессии или None (ретро-старт)
            "kills": int(match_summary.get("kills", 0)),
            "deaths": int(match_summary.get("deaths", 0)),
            "assists": int(match_summary.get("assists", 0)),
            "game_mode": game_mode,
            "trades": int(match_summary.get("trades", 0)),
            "duration_seconds": int(match_summary.get("duration_seconds", 0)),
            "created_at": float(created_at)
        }

        with self.lock:
            # 1. Всегда первично фиксируем на диск в pending_matches.json
            if not any(item.get("event_id") == event_id for item in self.retry_queue):
                self.retry_queue.append(payload)
                self._persist_queue()
                log.info(f"[NET:QUEUE_ADD] Матч {event_id} сохранен на диск в pending_matches.json (в очереди: {len(self.retry_queue)})")

        # 2. Если конфликт не активен и очередь содержит только этот матч — пробуем отправить немедленно
        if not self.manual_conflict:
            with self.lock:
                should_immediate = len(self.retry_queue) == 1
            if should_immediate:
                ok, res_or_err = self._post_payload(payload)
                if ok:
                    with self.lock:
                        self.retry_queue = [x for x in self.retry_queue if x.get("event_id") != event_id]
                        self._persist_queue()
                    log.info(f"[NET:RESP_OK] Матч {event_id} успешно доставлен на сервер: K={payload['kills']}, D={payload['deaths']}")
                    return True, res_or_err
                else:
                    self._handle_delivery_error(payload, res_or_err)
                    return False, res_or_err

        # Будим воркер для доставки в фоне
        self.wake_event.set()
        return True, {"queued": True, "event_id": event_id}

    def send_match_state_pulse(self, state="in_combat", kills=0, deaths=0):
        """
        Отправляет промежуточный боевой статус на сервер без создания потоков
        через единую неблокирующую очередь state_queue.
        """
        payload = {
            "type": "match_state",
            "state": state,
            "kills": int(kills),
            "deaths": int(deaths)
        }
        sid = self.get_current_session_id()
        if sid:
            payload["session_id"] = sid
        try:
            self.state_queue.put_nowait(payload)
        except queue.Full:
            pass

    def _state_worker_loop(self):
        """Поток отправки боевых статусов (in_combat / in_lobby)."""
        while self.running:
            try:
                payload = self.state_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if not self.running:
                break

            # Если активен manual_conflict — пропускаем импульсы, сервер вернет 409
            if self.manual_conflict:
                self.state_queue.task_done()
                continue

            try:
                self._post_payload(payload)
            except Exception as e:
                log.warning(f"[NET:STATE_WORKER_ERR] Ошибка отправки статуса {payload.get('type')}: {e}")
            finally:
                self.state_queue.task_done()

    def _update_queue_item(self, event_id, **fields):
        """Обновляет запись очереди по event_id и сохраняет её на диск."""
        with self.lock:
            for it in self.retry_queue:
                if it.get("event_id") == event_id:
                    it.update(fields)
                    break
            self._persist_queue()

    def _mark_permanent(self, item, reason, code):
        """Помечает матч как неустранимую ошибку: автоматических повторов больше нет."""
        event_id = item.get("event_id")
        self._update_queue_item(
            event_id,
            permanent_fail=True,
            fail_reason=str(reason)[:200],
            fail_code=code,
        )
        state = self.get_delivery_state()
        log.error(
            f"[NET:PERMANENT_FAIL] Матч {event_id} не будет отправлен автоматически "
            f"(код {code}): {reason}. Всего таких матчей: {state['failed_count']}"
        )

    def _handle_delivery_error(self, item, err_info):
        """Разбор ошибки отправки: ожидание, постоянные и временные ошибки (этап 16)."""
        event_id = item.get("event_id")
        if isinstance(err_info, dict):
            if err_info.get("manual_conflict"):
                self.manual_conflict = True
                self.backoff_idx = 0
                log.warning(f"[NET:MANUAL_CONFLICT] На сайте активна ручная сессия. Отправка приостановлена, матч {event_id} остаётся в очереди.")
                return

            if err_info.get("session_mismatch"):
                # session_mismatch -> удалить только этот конкретный матч из очереди, сбросить session_id и установить session_lost
                self.backoff_idx = 0
                with self.lock:
                    self.retry_queue = [x for x in self.retry_queue if x.get("event_id") != event_id]
                    self._persist_queue()
                    self.current_session_id = None
                    self.session_lost = True
                log.warning(f"[NET:SESSION_MISMATCH] Матч {event_id} отклонен сервером (сессия уже закрыта). Удален из очереди. Сессия сброшена.")
                return

            if err_info.get("no_active_session"):
                self.backoff_idx = 0
                if not self._waiting_session_logged:
                    log.info(f"[WAIT:SESSION] Ожидание старта сессии: в очереди {len(self.retry_queue)} матчей (штатное ожидание, не ошибка)")
                    self._waiting_session_logged = True
                return

            if err_info.get("token_error"):
                code = err_info.get("status")
                msg = err_info.get("error") or f"HTTP {code}"
                with self.lock:
                    self.token_error = {"code": code, "message": msg}
                    self.backoff_idx = 0
                log.error(
                    f"[NET:TOKEN_REJECTED] Сервер отклонил токен ({code}): {msg}. "
                    f"Отправка приостановлена до ручного повтора. Матчи в очереди не удаляются."
                )
                return

            if err_info.get("rate_limited"):
                delay = err_info.get("retry_after") or DEFAULT_RETRY_AFTER
                try:
                    delay = int(delay)
                except (TypeError, ValueError):
                    delay = DEFAULT_RETRY_AFTER
                delay = max(1, min(delay, MAX_RETRY_AFTER))
                with self.lock:
                    self.next_delay = delay
                log.warning(f"[NET:RATE_LIMIT] Сервер ограничил частоту (429). Пауза {delay}с по Retry-After.")
                return

            if err_info.get("permanent"):
                self._mark_permanent(item, err_info.get("error") or "Постоянная ошибка сервера", err_info.get("status"))
                return

        err_str = str(err_info).lower()
        if "нет активной сессии" in err_str:
            self.backoff_idx = 0
            if not self._waiting_session_logged:
                log.info(f"[WAIT:SESSION] Ожидание старта сессии: в очереди {len(self.retry_queue)} матчей (штатное ожидание, не ошибка)")
                self._waiting_session_logged = True
            return

        # Сетевые ошибки или 5xx -> экспоненциальный бэкофф, но не бесконечно
        with self.lock:
            for it in self.retry_queue:
                if it.get("event_id") == event_id:
                    attempts = int(it.get("attempts") or 0) + 1
                    it["attempts"] = attempts
                    break
            else:
                attempts = 0
            self._persist_queue()

        if attempts >= MAX_ATTEMPTS:
            self._mark_permanent(item, f"не удалось отправить за {attempts} попыток: {err_info}", None)
            return

        self._waiting_session_logged = False
        delay = BACKOFF_STAGES[self.backoff_idx]
        self.backoff_idx = min(self.backoff_idx + 1, len(BACKOFF_STAGES) - 1)
        log.warning(f"[NET:BACKOFF] Ошибка доставки ({err_info}). Повтор через {delay}с (бэкофф шаг {self.backoff_idx}/{len(BACKOFF_STAGES)})")

    def _post_payload(self, payload, is_retry=False):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            log.warning("[NET:SKIP] Отправка отменена: sync_token не привязан")
            return False, {"error": "Токен синхронизации не указан в настройках"}

        api_url = f"{config.get('api_base_url', 'https://kdmetrika.ru')}/api/tracker/pulse"
        req_start = time.monotonic()
        p_type = payload.get("type", "unknown")
        masked_tok = logger.mask_token(token)

        silent = is_retry and self._waiting_session_logged
        if not silent:
            log.info(f"[NET:REQ] POST {api_url} [tok={masked_tok}] type='{p_type}' payload={json.dumps(payload, ensure_ascii=False)}")

        try:
            req_data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                api_url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "X-Sync-Token": token
                }
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                elapsed_ms = int((time.monotonic() - req_start) * 1000)
                resp_json = json.loads(resp.read().decode('utf-8'))
                if resp_json.get("success"):
                    # Сервер вернул success: true (включая duplicate_prevented: true)
                    log.info(f"[NET:RESP] 200 OK ({elapsed_ms}ms) type='{p_type}': {json.dumps(resp_json, ensure_ascii=False)}")
                    return True, resp_json
                err_msg = resp_json.get("error", "Ошибка API")
                if not (silent and "нет активной сессии" in str(err_msg).lower()):
                    log.warning(f"[NET:RESP_FAIL] ({elapsed_ms}ms) type='{p_type}': {err_msg}")
                return False, {"error": err_msg}
        except urllib.error.HTTPError as e:
            elapsed_ms = int((time.monotonic() - req_start) * 1000)
            err_data = {}
            try:
                err_data = json.loads(e.read().decode('utf-8'))
            except Exception:
                pass
            err_msg = err_data.get("error", f"HTTP {e.code}")

            if e.code == 409:
                if err_data.get("manual_conflict"):
                    return False, {"manual_conflict": True, "error": err_msg, "status": 409}
                if err_data.get("session_mismatch"):
                    return False, {"session_mismatch": True, "error": err_msg, "status": 409}

            if e.code == 400 and "нет активной сессии" in str(err_msg).lower():
                return False, {"no_active_session": True, "error": err_msg, "status": 400}

            if e.code in (401, 403):
                log.error(f"[NET:TOKEN_ERR] ({elapsed_ms}ms) type='{p_type}': сервер отклонил токен ({e.code}): {err_msg}")
                return False, {"token_error": True, "error": err_msg, "status": e.code}

            if e.code == 429:
                retry_after = None
                try:
                    raw_ra = e.headers.get("Retry-After") if e.headers else None
                    if raw_ra is not None:
                        retry_after = int(str(raw_ra).strip())
                except (TypeError, ValueError):
                    retry_after = None
                log.warning(f"[NET:HTTP_429] ({elapsed_ms}ms) type='{p_type}': лимит частоты, Retry-After={retry_after}")
                return False, {"rate_limited": True, "retry_after": retry_after, "error": err_msg, "status": 429}

            if e.code >= 500:
                return False, {"is_server_error": True, "error": err_msg, "status": e.code}

            if 400 <= e.code < 500:
                log.error(f"[NET:HTTP_ERR] ({elapsed_ms}ms) type='{p_type}': постоянная ошибка {e.code}: {err_msg}")
                return False, {"permanent": True, "error": err_msg, "status": e.code}

            if not (silent and "нет активной сессии" in str(err_msg).lower()):
                log.error(f"[NET:HTTP_ERR] ({elapsed_ms}ms) type='{p_type}': {err_msg}")
            return False, {"error": err_msg, "status": e.code}
        except Exception as e:
            elapsed_ms = int((time.monotonic() - req_start) * 1000)
            log.error(f"[NET:CONN_ERR] ({elapsed_ms}ms) type='{p_type}': {e}")
            return False, {"is_network_error": True, "error": f"Ошибка сети: {e}"}

    def _retry_worker_loop(self):
        """Единый фоновый поток доставки очереди матчей."""
        while self.running:
            # Расчет задержки: пауза по Retry-After важнее бэкоффа
            with self.lock:
                forced_delay = self.next_delay
                self.next_delay = None
                token_error = bool(self.token_error)
            delay = forced_delay if forced_delay else (BACKOFF_STAGES[self.backoff_idx] if self.backoff_idx > 0 else 3.0)
            self.wake_event.wait(timeout=delay)
            self.wake_event.clear()

            if not self.running:
                break

            if token_error:
                # Токен отклонён сервером: не долбим сервер, ждём ручного повтора
                continue

            with self.lock:
                # 4-часовой лимит: отсекаем старые матчи
                now = time.time()
                kept = []
                expired = []
                for item in self.retry_queue:
                    created = float(item.get("created_at") or now)
                    if (now - created) > MAX_QUEUE_AGE:
                        expired.append(item)
                    else:
                        kept.append(item)
                if expired:
                    log.warning(f"[NET:EXPIRED] В цикле воркера отброшено {len(expired)} устаревших матчей (>4ч)")
                    self.retry_queue = kept
                    self._persist_queue()

                if not self.retry_queue:
                    self._waiting_session_logged = False
                    self.backoff_idx = 0
                    continue

                if self.manual_conflict:
                    continue

                # Берём первый матч, который не помечен как неустранимая ошибка
                item = None
                for candidate in self.retry_queue:
                    if not candidate.get("permanent_fail"):
                        item = dict(candidate)
                        break
                if item is None:
                    continue

            # Попытка отправки головного элемента (FIFO)
            ok, res_or_err = self._post_payload(item, is_retry=True)
            if ok:
                with self.lock:
                    self.retry_queue = [x for x in self.retry_queue if x.get("event_id") != item.get("event_id")]
                    self._persist_queue()
                self.backoff_idx = 0
                self._waiting_session_logged = False
                log.info(f"[RETRY:OK] Матч {item.get('event_id')} успешно доставлен из очереди повторов")
                # Если в очереди еще остались матчи — сразу будим воркер для следующего
                with self.lock:
                    if self.retry_queue:
                        self.wake_event.set()
            else:
                self._handle_delivery_error(item, res_or_err)

    def flush_pending(self, timeout=10.0):
        """
        Принудительная досылка неотправленных матчей с таймаутом (по умолчанию 10с).
        Возвращает количество оставшихся в очереди матчей.
        """
        start_time = time.monotonic()
        while self.running and (time.monotonic() - start_time) < timeout:
            with self.lock:
                if not self.retry_queue:
                    return 0
                if self.token_error:
                    break
                item = None
                for candidate in self.retry_queue:
                    if not candidate.get("permanent_fail"):
                        item = dict(candidate)
                        break
                if item is None:
                    break

            ok, res_or_err = self._post_payload(item, is_retry=True)
            if ok:
                with self.lock:
                    self.retry_queue = [x for x in self.retry_queue if x.get("event_id") != item.get("event_id")]
                    self._persist_queue()
                self.backoff_idx = 0
                continue

            if isinstance(res_or_err, dict):
                if res_or_err.get("session_mismatch"):
                    with self.lock:
                        if self.retry_queue and self.retry_queue[0].get("event_id") == item.get("event_id"):
                            self.retry_queue.pop(0)
                            self._persist_queue()
                        self.current_session_id = None
                        self.session_lost = True
                    continue
                if res_or_err.get("manual_conflict"):
                    self.manual_conflict = True
                    break
                if res_or_err.get("no_active_session"):
                    break
                if res_or_err.get("token_error"):
                    self._handle_delivery_error(item, res_or_err)
                    break
                if res_or_err.get("rate_limited"):
                    self._handle_delivery_error(item, res_or_err)
                    break
                if res_or_err.get("permanent"):
                    self._handle_delivery_error(item, res_or_err)
                    continue

            err_str = str(res_or_err).lower()
            if "нет активной сессии" in err_str:
                break

            # При сетевой ошибке делаем короткую паузу
            time.sleep(1.0)

        with self.lock:
            return len(self.retry_queue)

    def stop(self):
        self.running = False
        self.wake_event.set()
