import os
import time
import json
import uuid
import threading
import urllib.request
import urllib.error
import config_manager
import logger

log = logger.get_logger("Network")


class NetworkSync:
    """
    Модуль сетевой синхронизации с сайтом KD Metrika.
    Обеспечивает гарантированную доставку импульсов матчей
    с защитой от дублирования (Idempotency Key / event_id)
    и очередью повторных попыток при сетевых сбоях.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.retry_queue = []
        self._waiting_session_logged = False
        self.running = True
        self.worker_thread = threading.Thread(target=self._retry_worker, daemon=True)
        self.worker_thread.start()

    def get_pending_matches(self, max_age_seconds=14400):
        """Возвращает список накопленных матчей за последние 4 часа, отсекая устаревшие."""
        with self.lock:
            now = time.time()
            self.retry_queue = [
                item for item in self.retry_queue
                if (now - float(item.get("created_at", now))) <= max_age_seconds
            ]
            return list(self.retry_queue)

    def clear_pending_matches(self):
        """Принудительно очищает очередь повторов (при сбросе или завершении сессии)."""
        with self.lock:
            cleared = len(self.retry_queue)
            self.retry_queue.clear()
            self._waiting_session_logged = False
        if cleared > 0:
            log.info(f"[NET:QUEUE_CLEAR] Буфер неотправленных матчей очищен (удалено {cleared} матчей)")

    def send_match_pulse(self, match_summary):
        """
        Отправляет завершенный матч на сервер.
        Если связи нет, ставит в очередь на повтор.
        """
        event_id = match_summary.get("match_id") or f"evt_{uuid.uuid4().hex[:12]}"
        payload = {
            "type": "match",
            "event_id": event_id,
            "match_id": match_summary.get("match_id"),
            "kills": int(match_summary.get("kills", 0)),
            "deaths": int(match_summary.get("deaths", 0)),
            "assists": int(match_summary.get("assists", 0)),
            "game_mode": "BountyHuntLobby",
            "duration_seconds": int(match_summary.get("duration_seconds", 0)),
            "created_at": time.time()
        }

        success, resp_or_err = self._post_payload(payload)
        if not success:
            log.warning(f"Ошибка немедленной отправки импульса {event_id}: {resp_or_err}. Помещен в очередь повторов.")
            with self.lock:
                # Добавляем в очередь повторов, если еще нет в очереди
                if not any(item.get("event_id") == event_id for item in self.retry_queue):
                    self.retry_queue.append(payload)
            return False, resp_or_err

        log.info(f"Импульс матча {event_id} успешно доставлен на сервер: K={payload['kills']}, D={payload['deaths']}")
        return True, resp_or_err

    def send_match_state_pulse(self, state="in_combat", kills=0, deaths=0):
        """
        Отправляет промежуточный боевой статус на сервер (в бою / в лобби, киллы/смерти).
        Выполняется неблокирующе в отдельном потоке.
        """
        def _send():
            payload = {
                "type": "match_state",
                "state": state,
                "kills": int(kills),
                "deaths": int(deaths)
            }
            self._post_payload(payload)

        t = threading.Thread(target=_send, daemon=True)
        t.start()

    def _post_payload(self, payload, is_retry=False):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            log.warning("[NET:SKIP] Отправка отменена: sync_token не привязан")
            return False, "Токен синхронизации не указан в настройках"

        api_url = f"{config.get('api_base_url', 'https://kdmetrika.ru')}/api/tracker/pulse"
        req_start = time.monotonic()
        p_type = payload.get("type", "unknown")
        masked_tok = logger.mask_token(token)

        # Не спамим лог запросом, если это фоновый повтор в режиме ожидания сессии
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
                    log.info(f"[NET:RESP] 200 OK ({elapsed_ms}ms) type='{p_type}': {json.dumps(resp_json, ensure_ascii=False)}")
                    return True, resp_json
                err_msg = resp_json.get("error", "Ошибка API")
                if not (silent and "нет активной сессии" in str(err_msg).lower()):
                    log.warning(f"[NET:RESP_FAIL] ({elapsed_ms}ms) type='{p_type}': {err_msg}")
                return False, err_msg
        except urllib.error.HTTPError as e:
            elapsed_ms = int((time.monotonic() - req_start) * 1000)
            try:
                err_data = json.loads(e.read().decode('utf-8'))
                err_msg = err_data.get("error", f"HTTP {e.code}")
            except Exception:
                err_msg = f"HTTP Error {e.code}"
            if not (silent and "нет активной сессии" in str(err_msg).lower()):
                log.error(f"[NET:HTTP_ERR] ({elapsed_ms}ms) type='{p_type}': {err_msg}")
            return False, err_msg
        except Exception as e:
            elapsed_ms = int((time.monotonic() - req_start) * 1000)
            log.error(f"[NET:CONN_ERR] ({elapsed_ms}ms) type='{p_type}': {e}")
            return False, f"Ошибка сети: {e}"

    def _retry_worker(self):
        while self.running:
            time.sleep(3)
            with self.lock:
                now = time.time()
                # 4-часовой лимит: отсекаем старые матчи из буфера
                self.retry_queue = [
                    item for item in self.retry_queue
                    if (now - float(item.get("created_at", now))) <= 14400
                ]
                if not self.retry_queue:
                    self._waiting_session_logged = False
                    continue
                pending_items = list(self.retry_queue)

            delivered_ids = []
            for item in pending_items:
                ok, resp_or_err = self._post_payload(item, is_retry=True)
                if ok:
                    delivered_ids.append(item.get("event_id"))
                else:
                    err_str = str(resp_or_err).lower()
                    if "нет активной сессии" in err_str:
                        if not self._waiting_session_logged:
                            log.info(f"[WAIT:SESSION] Ожидание старта сессии: в буфере {len(pending_items)} матчей (опрос каждые 3с)")
                            self._waiting_session_logged = True
                    else:
                        # Реальная сетевая или серверная ошибка - сбрасываем тихий режим и фиксируем
                        self._waiting_session_logged = False
                    break

            if delivered_ids:
                if self._waiting_session_logged:
                    log.info(f"[RETRY:OK] Сессия обнаружена, успешно доставлены отложенные импульсы ({len(delivered_ids)} матчей): {delivered_ids}")
                    self._waiting_session_logged = False
                else:
                    log.info(f"Успешно доставлены из очереди повторов импульсы: {delivered_ids}")
                with self.lock:
                    self.retry_queue = [
                        item for item in self.retry_queue
                        if item.get("event_id") not in delivered_ids
                    ]

    def stop(self):
        self.running = False
