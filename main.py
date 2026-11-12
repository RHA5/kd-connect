import os
import sys
import json
import time
import urllib.request
import urllib.error

APP_VERSION = "v0.9.12-beta"
GITHUB_REPO = "RHA5/kd-connect"

# Привязка приложения к Windows Taskbar для четкой иконки
if sys.platform == 'win32':
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('kdmetrika.kdconnect.companion.1.0')
    except Exception:
        pass

# Защита от краша в режиме без консоли (PyInstaller --noconsole)
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

import webview
import config_manager
import hunt_detector
import logger
from log_tailer import LogTailer, DynamicNvidiaLogTailer
from tracker_engine import TrackerEngine

log = logger.get_logger("Main")


def on_session_change(is_active, data):
    if webview.windows:
        try:
            fn = "onEngineSessionStart" if is_active else "onEngineSessionEnd"
            webview.windows[0].evaluate_js(f"window.{fn} && window.{fn}({json.dumps(data)})")
        except Exception:
            pass


def on_combat_event(event_type, count, stats):
    if webview.windows:
        try:
            webview.windows[0].evaluate_js(f"window.onEngineCombatEvent && window.onEngineCombatEvent('{event_type}', {count}, {json.dumps(stats)})")
        except Exception:
            pass


def _parse_username(data):
    if not isinstance(data, dict):
        return ""
    user_val = data.get("user")
    if isinstance(user_val, dict):
        return str(user_val.get("username") or user_val.get("name") or "")
    if isinstance(user_val, str) and user_val.strip():
        return user_val.strip()
    raw_name = data.get("username")
    if isinstance(raw_name, str) and raw_name.strip():
        return raw_name.strip()
    return ""


class AppApi:
    def __init__(self):
        self.engine = None

    def set_engine(self, engine):
        self.engine = engine

    def get_pending_matches(self):
        try:
            if not self.engine or not hasattr(self.engine, 'network_sync'):
                return {"has_pending": False, "count": 0, "kills": 0, "deaths": 0}
            matches = self.engine.network_sync.get_pending_matches()
            if not matches:
                return {"has_pending": False, "count": 0, "kills": 0, "deaths": 0}

            count = len(matches)
            kills = sum(int(m.get("kills", 0)) for m in matches)
            deaths = sum(int(m.get("deaths", 0)) for m in matches)

            # Вычисляем самое раннее время начала матча
            earliest_start = min(
                float(m.get("created_at", time.time())) - float(m.get("duration_seconds", 0))
                for m in matches
            )
            import datetime
            iso_start = datetime.datetime.fromtimestamp(earliest_start, datetime.timezone.utc).isoformat()

            return {
                "has_pending": True,
                "count": count,
                "kills": kills,
                "deaths": deaths,
                "earliest_start": earliest_start,
                "retroactive_created_at": iso_start
            }
        except Exception as e:
            log.error(f"[PENDING:ERR] {e}")
            return {"has_pending": False, "count": 0, "kills": 0, "deaths": 0}

    def clear_pending_matches(self):
        try:
            if self.engine and hasattr(self.engine, 'network_sync'):
                self.engine.network_sync.clear_pending_matches()
            return {"success": True}
        except Exception as e:
            log.error(f"[CLEAR:ERR] {e}")
            return {"success": False, "error": str(e)}

    def minimize(self):
        if webview.windows:
            webview.windows[0].minimize()

    def close(self):
        if webview.windows:
            webview.windows[0].destroy()

    def select_custom_folder(self):
        if not webview.windows:
            return {"success": False}
        res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if res and len(res) > 0:
            chosen = res[0]
            config_manager.update_config_value("custom_hunt_path", chosen)
            return {"success": True, "path": chosen}
        return {"success": False, "canceled": True}

    def reset_custom_folder(self):
        config_manager.update_config_value("custom_hunt_path", "")
        return self.check_systems()

    def check_systems(self):
        config = config_manager.load_config()
        custom_path = config.get("custom_hunt_path")
        
        # 1. Hunt Game Detection
        hunt_dir, game_log, hunt_status = hunt_detector.detect_hunt_directory(custom_path)
        
        if custom_path:
            if hunt_status == 'ok':
                hunt_msg = hunt_dir
            elif hunt_status == 'log_missing_yet':
                hunt_msg = "Ожидание первого запуска игры"
            else:
                hunt_msg = "В указанной папке нет Hunt: Showdown"
        else:
            if hunt_status == 'ok':
                hunt_msg = hunt_dir
            elif hunt_status == 'log_missing_yet':
                hunt_msg = "Найдено в Steam (ожидание первого запуска)"
            else:
                hunt_msg = "Игра не найдена в библиотеках Steam"

        # 2. NVIDIA Overlay Log Detection
        nv_log, nv_status, nv_msg = hunt_detector.detect_nvidia_log()

        # 3. Profile Token Validation
        token = config.get("sync_token", "").strip()
        profile_info = {
            "status": "warning",
            "username": "",
            "token": token,
            "active_session": None,
            "manual_conflict": False
        }
        if token:
            api_url = f"{config.get('api_base_url', 'http://localhost:3000')}/api/tracker/pulse"
            try:
                req = urllib.request.Request(api_url, headers={"x-sync-token": token})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    resp_data = json.loads(resp.read().decode())
                    if resp_data.get("success"):
                        profile_info["status"] = "ok"
                        profile_info["username"] = _parse_username(resp_data)
                        profile_info["active_session"] = resp_data.get("active_session")
                        profile_info["manual_conflict"] = resp_data.get("manual_conflict", False)
                        profile_info["ask_mood_on_start"] = resp_data.get("ask_mood_on_start", False)
                        profile_info["base_kills"] = resp_data.get("base_kills", 0)
                        profile_info["base_deaths"] = resp_data.get("base_deaths", 0)
                    else:
                        profile_info["status"] = "error"
            except Exception:
                profile_info["status"] = "error"

        return {
            "config": config,
            "game": {
                "status": "ok" if hunt_status == "ok" else ("warning" if hunt_status == "log_missing_yet" else "error"),
                "dir": hunt_dir or "",
                "log_path": game_log or "",
                "message": hunt_msg
            },
            "nvidia": {
                "status": nv_status,
                "log_path": nv_log or "",
                "message": nv_msg
            },
            "profile": profile_info
        }

    def unlink_token(self):
        config_manager.update_config_value("sync_token", "")
        return {"success": True}

    def link_token(self, token):
        token = (token or "").strip()
        if not token:
            config_manager.update_config_value("sync_token", "")
            return {"success": False, "unlinked": True, "error": "Токен удален (аккаунт отвязан)"}

        # Сразу сохраняем введенный пользователем токен в конфиг, чтобы старый не воскресал
        config_manager.update_config_value("sync_token", token)

        config = config_manager.load_config()
        api_url = f"{config.get('api_base_url', 'http://localhost:3000')}/api/tracker/pulse"
        try:
            req = urllib.request.Request(api_url, headers={"x-sync-token": token})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success"):
                    username = _parse_username(data)
                    return {"success": True, "username": username}
                return {"success": False, "error": data.get("error", "Неверный токен тестера")}
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode())
                return {"success": False, "error": err_data.get("error", f"Недействительный токен ({e.code})")}
            except Exception:
                return {"success": False, "error": f"Ошибка авторизации ({e.code})"}
        except Exception:
            return {"success": False, "error": "Сервер недоступен (проверьте подключение)"}

    def start_session(self, mood_rating=None, retroactive_created_at=None):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            log.warning("[SESSION:START_FAIL] Попытка старта сессии без sync_token")
            return {"success": False, "error": "Привяжите токен перед началом сессии"}

        api_url = f"{config.get('api_base_url', 'http://localhost:3000')}/api/tracker/pulse"
        payload = {"type": "session_start"}
        if mood_rating is not None:
            payload["mood_rating"] = mood_rating
        if retroactive_created_at:
            payload["created_at"] = retroactive_created_at
        masked_tok = logger.mask_token(token)
        log.info(f"[SESSION:START_REQ] Старт сессии в БД [tok={masked_tok}, mood={mood_rating}, retro={retroactive_created_at}]")
        try:
            req_data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                api_url,
                data=req_data,
                headers={"Content-Type": "application/json", "X-Sync-Token": token},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data.get("success"):
                    log.info(f"[SESSION:START_OK] Сессия создана: ID={data.get('session_id')}, пользователь='{data.get('username')}', начальные K/D={data.get('base_kd')}")
                else:
                    log.warning(f"[SESSION:START_ERR] Ошибка создания: {data.get('error')}")
                return data
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode('utf-8'))
                err_msg = err.get("error", f"Ошибка HTTP {e.code}")
            except Exception:
                err_msg = f"Ошибка сервера ({e.code})"
            log.error(f"[SESSION:START_HTTP_ERR] {err_msg}")
            return {"success": False, "error": err_msg}
        except Exception as ex:
            log.error(f"[SESSION:START_CONN_ERR] {ex}")
            return {"success": False, "error": f"Сбой соединения: {ex}"}

    def end_session(self, adjusted_kills=None, adjusted_deaths=None):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            log.warning("[SESSION:END_FAIL] Попытка завершения сессии без sync_token")
            return {"success": False, "error": "Токен не найден"}

        api_url = f"{config.get('api_base_url', 'http://localhost:3000')}/api/tracker/pulse"
        payload = {"type": "session_end"}
        if adjusted_kills is not None:
            payload["adjusted_kills"] = adjusted_kills
        if adjusted_deaths is not None:
            payload["adjusted_deaths"] = adjusted_deaths
        masked_tok = logger.mask_token(token)
        log.info(f"[SESSION:END_REQ] Завершение сессии [tok={masked_tok}, adjK={adjusted_kills}, adjD={adjusted_deaths}]")
        try:
            # Очищаем буфер очереди, чтобы старые события гарантированно не перетекли в следующую сессию
            self.clear_pending_matches()

            req_data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                api_url,
                data=req_data,
                headers={"Content-Type": "application/json", "X-Sync-Token": token},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data.get("success"):
                    log.info(f"[SESSION:END_OK] Сессия завершена: ID={data.get('session_id')}, матчей={data.get('matches_count')}, киллов={data.get('kills')}, смертей={data.get('deaths')}, итоговые в лобби={data.get('total_kills')}/{data.get('total_deaths')}")
                else:
                    log.warning(f"[SESSION:END_ERR] Ошибка завершения: {data.get('error')}")
                return data
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode('utf-8'))
                err_msg = err.get("error", f"Ошибка HTTP {e.code}")
            except Exception:
                err_msg = f"Ошибка сервера ({e.code})"
            log.error(f"[SESSION:END_HTTP_ERR] {err_msg}")
            return {"success": False, "error": err_msg}
        except Exception as ex:
            log.error(f"[SESSION:END_CONN_ERR] {ex}")
            return {"success": False, "error": f"Сбой соединения: {ex}"}

    def check_active_session(self):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            return {"success": False, "active_session": None, "manual_conflict": False}
        api_url = f"{config.get('api_base_url', 'http://localhost:3000')}/api/tracker/pulse"
        try:
            req = urllib.request.Request(api_url, headers={"x-sync-token": token})
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp_data = json.loads(resp.read().decode())
                if resp_data.get("success"):
                    return {
                        "success": True,
                        "active_session": resp_data.get("active_session"),
                        "manual_conflict": resp_data.get("manual_conflict", False),
                        "ask_mood_on_start": resp_data.get("ask_mood_on_start", False),
                        "base_kills": resp_data.get("base_kills", 0),
                        "base_deaths": resp_data.get("base_deaths", 0)
                    }
                return {"success": False, "active_session": None, "manual_conflict": False}
        except Exception:
            return {"success": False, "active_session": None, "manual_conflict": False}

    def open_external_url(self, url):
        try:
            import webbrowser
            webbrowser.open(url)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_hide_disclaimer(self, val):
        config_manager.update_config_value("hide_disclaimer", bool(val))
        return {"success": True}

    def is_disclaimer_hidden(self):
        config = config_manager.load_config()
        return bool(config.get("hide_disclaimer", False))

    def reset_onboarding(self):
        config_manager.update_config_value("onboarding_completed", False)
        config_manager.update_config_value("hide_disclaimer", False)
        config_manager.update_config_value("sync_token", "")
        return self.check_systems()

    def complete_onboarding(self):
        config_manager.update_config_value("onboarding_completed", True)
        return {"success": True}

    def is_onboarding_completed(self):
        config = config_manager.load_config()
        return bool(config.get("onboarding_completed", False))

    def _get_system_diagnostics(self):
        try:
            config = config_manager.load_config()
            token = config.get("sync_token", "").strip()
            masked_token = f"{token[:8]}...{token[-4:]}" if len(token) > 12 else (token or "не привязан")
            hunt_dir, game_log, hunt_status = hunt_detector.detect_hunt_directory(config.get("custom_hunt_path"))
            nv_log, nv_status, nv_msg = hunt_detector.detect_nvidia_log()
            return (
                f"\n=== СИСТЕМНАЯ ДИАГНОСТИКА KD CONNECT ===\n"
                f"Платформа: {sys.platform}\n"
                f"Статус NVIDIA: {nv_status} ({nv_msg}) | Лог: {nv_log}\n"
                f"Статус Hunt: {hunt_status} | Папка: {hunt_dir} | Game.log: {game_log}\n"
                f"Токен: {masked_token}\n"
                f"API URL: {config.get('api_base_url')}\n"
                f"=========================================\n\n"
            )
        except Exception as e:
            return f"\n=== ДИАГНОСТИКА ОШИБКА: {e} ===\n\n"

    def send_report(self, message=""):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            return {"success": False, "error": "Привяжите токен в настройках перед отправкой отчета"}

        api_url = f"{config.get('api_base_url', 'http://localhost:3000')}/api/tracker/report"
        diagnostics = self._get_system_diagnostics()
        recent_logs = diagnostics + logger.get_recent_logs()

        payload = {
            "sync_token": token,
            "message": message or "Пользовательский отчет об ошибке из KD Connect",
            "log": recent_logs
        }
        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            api_url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "x-sync-token": token
            },
            method="POST"
        )
        try:
            log.info("Отправка отчета разработчикам...")
            with urllib.request.urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode())
                if res.get("success"):
                    log.info("Отчет успешно отправлен на сервер")
                    return {"success": True, "message": res.get("message", "Отчет успешно доставлен")}
                log.warning(f"Сервер отклонил отчет: {res.get('error')}")
                return {"success": False, "error": res.get("error", "Ошибка отправки отчета")}
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode())
                err_msg = err_data.get("error", f"Ошибка HTTP {e.code}")
            except Exception:
                err_msg = f"Ошибка сервера ({e.code})"
            log.error(f"HTTP ошибка при отправке отчета: {err_msg}")
            return {"success": False, "error": err_msg}
        except Exception as ex:
            log.error(f"Сбой соединения при отправке отчета: {ex}")
            return {"success": False, "error": "Сервер недоступен (проверьте подключение)"}

    def get_version(self):
        return APP_VERSION

    def check_for_updates(self):
        try:
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(api_url, headers={
                "User-Agent": f"KD-Connect/{APP_VERSION}",
                "Accept": "application/vnd.github.v3+json"
            })
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                latest_tag = data.get("tag_name", "").strip()
                if not latest_tag:
                    return {"update_available": False}

                download_url = None
                for asset in data.get("assets", []):
                    if asset.get("name", "").lower().endswith(".exe"):
                        download_url = asset.get("browser_download_url")
                        break

                if not download_url:
                    download_url = f"https://github.com/{GITHUB_REPO}/releases/latest/download/KD-Connect.exe"

                is_newer = (latest_tag != APP_VERSION)
                return {
                    "update_available": is_newer,
                    "current_version": APP_VERSION,
                    "latest_version": latest_tag,
                    "release_name": data.get("name", latest_tag),
                    "download_url": download_url
                }
        except Exception as e:
            return {"update_available": False, "error": str(e)}

    def apply_update(self, download_url=None):
        if not download_url:
            download_url = f"https://github.com/{GITHUB_REPO}/releases/latest/download/KD-Connect.exe"
        try:
            temp_dir = os.environ.get('TEMP', os.path.dirname(sys.executable))
            temp_exe = os.path.join(temp_dir, f"KD-Connect-update-{int(time.time())}.exe")

            req = urllib.request.Request(download_url, headers={"User-Agent": f"KD-Connect/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(temp_exe, 'wb') as out_f:
                out_f.write(resp.read())

            if not os.path.exists(temp_exe) or os.path.getsize(temp_exe) < 1000000:
                return {"success": False, "error": "Ошибка целостности скачанного файла"}

            current_exe = os.path.abspath(sys.executable)
            if not getattr(sys, 'frozen', False):
                return {"success": True, "downloaded_path": temp_exe, "dev_mode": True}

            batch_cmd = f'timeout /t 1 /nobreak >nul & move /y "{temp_exe}" "{current_exe}" & start "" "{current_exe}"'
            import subprocess
            subprocess.Popen(
                ["cmd.exe", "/c", batch_cmd],
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                close_fds=True
            )

            if webview.windows:
                webview.windows[0].destroy()
            sys.exit(0)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def export_log(self):
        try:
            target_path = logger.export_log()
            diagnostics = self._get_system_diagnostics()
            try:
                with open(target_path, 'r+', encoding='utf-8') as f:
                    content = f.read()
                    f.seek(0, 0)
                    f.write(diagnostics + content)
            except Exception:
                pass
            log.info(f"Логи успешно выгружены в {target_path}")
            return {"success": True, "path": target_path}
        except Exception as ex:
            log.error(f"Сбой экспорта логов: {ex}")
            return {"success": False, "error": str(ex)}


def get_asset_path(filename):
    if getattr(sys, 'frozen', False):
        base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, filename)


if __name__ == '__main__':
    try:
        api = AppApi()
        html_path = get_asset_path('preview.html')
        
        # Запасной путь, если лежит рядом с exe
        if not os.path.exists(html_path):
            exe_dir = os.path.dirname(sys.executable)
            alt_path = os.path.join(exe_dir, 'preview.html')
            if os.path.exists(alt_path):
                html_path = alt_path

        # Надежное чтение содержимого HTML в память
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        window = webview.create_window(
            'KD Connect',
            html=html_content,
            js_api=api,
            width=430,
            height=660,
            resizable=False,
            frameless=True,
            easy_drag=False,
            background_color='#09090b'
        )
        
        icon_path = get_asset_path('icon.ico')
        if not os.path.exists(icon_path):
            exe_dir = os.path.dirname(sys.executable)
            alt_icon = os.path.join(exe_dir, 'icon.ico')
            if os.path.exists(alt_icon):
                icon_path = alt_icon

        engine = TrackerEngine(
            on_session_change=on_session_change,
            on_combat_event=on_combat_event
        )
        api.set_engine(engine)
        engine.start()

        webview.start(debug=False, icon=icon_path if os.path.exists(icon_path) else None)
        engine.stop()
    except Exception as e:
        crash_path = os.path.join(os.path.dirname(sys.executable), 'crash.log')
        with open(crash_path, 'w', encoding='utf-8') as f:
            import traceback
            f.write(traceback.format_exc())
