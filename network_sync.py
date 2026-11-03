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
        self.running = True
        self.worker_thread = threading.Thread(target=self._retry_worker, daemon=True)
        self.worker_thread.start()

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

    def _post_payload(self, payload):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            return False, "Токен синхронизации не указан в настройках"

        api_url = f"{config.get('api_base_url', 'http://localhost:3000')}/api/tracker/pulse"
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
                resp_json = json.loads(resp.read().decode('utf-8'))
                if resp_json.get("success"):
                    return True, resp_json
                return False, resp_json.get("error", "Ошибка API")
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode('utf-8'))
                return False, err_data.get("error", f"HTTP {e.code}")
            except Exception:
                return False, f"HTTP Error {e.code}"
        except Exception as e:
            return False, f"Ошибка сети: {e}"

    def _retry_worker(self):
        while self.running:
            time.sleep(3)
            with self.lock:
                if not self.retry_queue:
                    continue
                pending_items = list(self.retry_queue)

            delivered_ids = []
            for item in pending_items:
                ok, _ = self._post_payload(item)
                if ok:
                    delivered_ids.append(item.get("event_id"))
                else:
                    # Если один не прошел (сервер лежит), прерываем цикл попыток до следующего такта
                    break

            if delivered_ids:
                log.info(f"Успешно доставлены из очереди повторов импульсы: {delivered_ids}")
                with self.lock:
                    self.retry_queue = [
                        item for item in self.retry_queue
                        if item.get("event_id") not in delivered_ids
                    ]

    def stop(self):
        self.running = False
