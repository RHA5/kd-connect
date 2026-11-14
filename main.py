import os
import re
import sys
import json
import time
import hashlib
import urllib.parse
import urllib.request
import urllib.error

APP_VERSION = "v0.10.6"

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

import faulthandler
import traceback
import threading

CRASH_LOG_PATH = os.path.join(config_manager.APP_DATA_DIR, 'crash.log')

def _write_crash_record(header, err_text):
    try:
        config_manager.ensure_app_data_dir()
        with open(CRASH_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f"\n=== {header} ===\n{err_text}\n" + "=" * 50 + "\n")
    except Exception:
        pass

def setup_crash_handlers():
    config_manager.ensure_app_data_dir()
    try:
        _crash_f = open(CRASH_LOG_PATH, 'a', encoding='utf-8')
        faulthandler.enable(_crash_f)
    except Exception:
        pass

    def _sys_excepthook(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        tname = threading.current_thread().name
        err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        _write_crash_record(f"CRASH: {ts} | Thread: {tname} (Main)", err_msg)
        log.critical(f"Фатальный сбой приложения:\n{err_msg}")
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = _sys_excepthook

    def _thread_excepthook(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        tname = args.thread.name if args.thread else "UnknownThread"
        err_msg = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        _write_crash_record(f"THREAD CRASH: {ts} | Thread: {tname}", err_msg)
        log.critical(f"Фатальный сбой в потоке {tname}:\n{err_msg}")

    threading.excepthook = _thread_excepthook

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


def _get_api_base_url():
    config = config_manager.load_config()
    raw = str(config.get("api_base_url") or "https://kdmetrika.ru").strip().rstrip("/")
    return raw or "https://kdmetrika.ru"


def _resolve_download_url(raw_url=None, base_url=None):
    base = (base_url or _get_api_base_url()).rstrip("/")
    target = str(raw_url or "/downloads/KD-Connect.exe").strip()
    return urllib.parse.urljoin(f"{base}/", target)


def _parse_semver(version_str):
    if not isinstance(version_str, str):
        return None
    cleaned = version_str.strip().lstrip("vV")
    if not cleaned:
        return None
    core_part, _, pre_part = cleaned.partition("-")
    core_segments = core_part.split(".")
    if not core_segments or any(not seg.isdigit() for seg in core_segments):
        return None
    core = tuple(int(seg) for seg in core_segments)
    while len(core) < 3:
        core = core + (0,)
    if not pre_part:
        return (core, 1, ())
    pre_tokens = tuple(
        (0, int(tok)) if tok.isdigit() else (1, tok.lower())
        for tok in re.split(r"[.\-]+", pre_part)
        if tok
    )
    return (core, 0, pre_tokens)


def _is_newer_version(candidate, current):
    parsed_candidate = _parse_semver(candidate)
    parsed_current = _parse_semver(current)
    if parsed_candidate is None or parsed_current is None:
        return bool(candidate and candidate.strip() != str(current or "").strip())
    return parsed_candidate > parsed_current


def _enable_taskbar_minimize(title="KD Connect"):
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            GWL_STYLE = -16
            WS_MINIMIZEBOX = 0x00020000
            WS_SYSMENU = 0x00080000
            current_style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            user32.SetWindowLongW(hwnd, GWL_STYLE, current_style | WS_MINIMIZEBOX | WS_SYSMENU)
    except Exception:
        pass


def _format_token_error(error_str, http_code=None):
    err = (error_str or "").lower()
    if any(k in err for k in ["недействительный токен", "не найден", "invalid token", "not found"]):
        return "Неверный токен синхронизации"
    if any(k in err for k in ["роль", "tester", "superadmin", "роли"]):
        return "Доступ ограничен (требуется роль бета-тестера)"
    if http_code in (401, 403):
        return "Доступ ограничен или неверный токен"
    if any(k in err for k in ["timeout", "сервер недоступен", "connection", "500", "502", "503", "504"]):
        return "Сервер синхронизации временно недоступен"
    return error_str or "Ошибка проверки токена"


def is_url_allowed(url, api_base_url):
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()

        # Localhost allowlist (может быть http или https)
        if hostname in ("localhost", "127.0.0.1"):
            return scheme in ("http", "https")

        # Все внешние URL строго HTTPS
        if scheme != "https":
            return False

        # Официальные ресурсы NVIDIA (оверлей Highlights)
        if hostname in ("nvidia.com", "www.nvidia.com"):
            return True

        # Поддержка Telegram
        if hostname in ("t.me", "telegram.me"):
            return True

        # Базовый домен API и его поддомены
        base_parsed = urllib.parse.urlparse(api_base_url)
        base_host = (base_parsed.hostname or "").lower()
        if base_host and (hostname == base_host or hostname.endswith("." + base_host)):
            return True

        return False
    except Exception:
        return False


def is_webview2_available():
    if sys.platform != 'win32':
        return True
    try:
        import winreg
        guids = [
            '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
            '{F3017226-9D4A-4474-9A14-9242E6018E60}',
            '{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}',
        ]
        roots = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
        subpaths = [
            r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients',
            r'SOFTWARE\Microsoft\EdgeUpdate\Clients',
            r'Software\WOW6432Node\Microsoft\EdgeUpdate\Clients',
            r'Software\Microsoft\EdgeUpdate\Clients',
        ]
        for root in roots:
            for subpath in subpaths:
                for guid in guids:
                    try:
                        with winreg.OpenKey(root, f"{subpath}\\{guid}") as k:
                            val, _ = winreg.QueryValueEx(k, 'pv')
                            if val and val != '0.0.0.0':
                                return True
                    except Exception:
                        pass
        return False
    except Exception:
        return False


class AppApi:
    def __init__(self):
        self.engine = None

    def set_engine(self, engine):
        self.engine = engine

    def on_frontend_ready(self):
        if sys.platform == 'win32' and webview.windows:
            try:
                form = webview.windows[0].native
                if form and hasattr(form, 'Controls') and form.Controls.Count > 0:
                    wv_ctrl = form.Controls[0]
                    from System.Windows.Forms import MethodInvoker
                    form.Invoke(MethodInvoker(lambda: setattr(wv_ctrl, 'Visible', True)))
            except Exception as ex:
                log.warning(f"on_frontend_ready error: {ex}")
        return True

    def show_window(self):
        if webview.windows:
            win = webview.windows[0]
            win.show()
            _enable_taskbar_minimize("KD Connect")
        return True

    def get_clipboard_text(self):
        if sys.platform != 'win32':
            return ""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.CloseClipboard.restype = wintypes.BOOL
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.restype = wintypes.BOOL

            if user32.OpenClipboard(None):
                h = user32.GetClipboardData(13)  # CF_UNICODETEXT
                val = ""
                if h:
                    p = kernel32.GlobalLock(h)
                    if p:
                        val = str(ctypes.c_wchar_p(p).value or "")
                        kernel32.GlobalUnlock(h)
                user32.CloseClipboard()
                return val
        except Exception:
            pass
        return ""

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
            "manual_conflict": False,
            "error": ""
        }
        if token:
            api_url = f"{_get_api_base_url()}/api/tracker/pulse"
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
                        profile_info["error"] = _format_token_error(resp_data.get("error", ""))
            except urllib.error.HTTPError as e:
                profile_info["status"] = "error"
                try:
                    err_json = json.loads(e.read().decode())
                    raw_err = err_json.get("error", "")
                except Exception:
                    raw_err = ""
                profile_info["error"] = _format_token_error(raw_err, http_code=e.code)
            except Exception:
                profile_info["status"] = "error"
                profile_info["error"] = "Сервер синхронизации временно недоступен"

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

        api_url = f"{_get_api_base_url()}/api/tracker/pulse"
        try:
            req = urllib.request.Request(api_url, headers={"x-sync-token": token})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success"):
                    username = _parse_username(data)
                    config_manager.update_config_value("sync_token", token)
                    return {"success": True, "username": username}
                err_msg = _format_token_error(data.get("error", "Неверный токен"))
                return {"success": False, "error": err_msg}
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode())
                raw_err = err_data.get("error", "")
            except Exception:
                raw_err = ""
            err_msg = _format_token_error(raw_err, http_code=e.code)
            return {"success": False, "error": err_msg}
        except Exception:
            return {"success": False, "error": "Сервер синхронизации временно недоступен"}

    def start_session(self, mood_rating=None, retroactive_created_at=None):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            log.warning("[SESSION:START_FAIL] Попытка старта сессии без sync_token")
            return {"success": False, "error": "Привяжите токен перед началом сессии"}

        api_url = f"{_get_api_base_url()}/api/tracker/pulse"
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
                    if self.engine and hasattr(self.engine, 'network_sync'):
                        self.engine.network_sync.set_current_session_id(data.get('session_id'))
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

    def end_session(self, adjusted_kills=None, adjusted_deaths=None, force_discard=False, force_in_combat=False):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            log.warning("[SESSION:END_FAIL] Попытка завершения сессии без sync_token")
            return {"success": False, "error": "Токен не найден"}

        # 1. Предупреждение при попытке завершить сессию во время идущего матча («В бою»)
        if self.engine and hasattr(self.engine, 'state_machine') and getattr(self.engine.state_machine, "session_active", False) and not force_in_combat:
            return {
                "success": False,
                "confirm_in_combat": True,
                "error": "Матч ещё идёт. Завершить сессию? Статистика текущей катки не попадёт в неё."
            }

        # 2. Принудительный flush очереди с таймаутом 10 с
        if self.engine and hasattr(self.engine, 'network_sync'):
            remaining = self.engine.network_sync.flush_pending(timeout=10.0)
            if remaining > 0 and not force_discard:
                return {
                    "success": False,
                    "confirm_pending_matches": True,
                    "pending_matches_count": remaining,
                    "error": f"В очереди {remaining} недоставленных матчей. Повторить попытку или завершить без них?"
                }
            if force_discard:
                # Осознанная очистка очереди по кнопке «Завершить без них»
                self.engine.network_sync.clear_pending_matches()

        api_url = f"{_get_api_base_url()}/api/tracker/pulse"
        payload = {"type": "session_end"}
        if adjusted_kills is not None:
            payload["adjusted_kills"] = adjusted_kills
        if adjusted_deaths is not None:
            payload["adjusted_deaths"] = adjusted_deaths
        masked_tok = logger.mask_token(token)
        log.info(f"[SESSION:END_REQ] Завершение сессии [tok={masked_tok}, adjK={adjusted_kills}, adjD={adjusted_deaths}]")
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
                    log.info(f"[SESSION:END_OK] Сессия завершена: ID={data.get('session_id')}, матчей={data.get('matches_count')}, киллов={data.get('kills')}, смертей={data.get('deaths')}, итоговые в лобби={data.get('total_kills')}/{data.get('total_deaths')}")
                    if self.engine and hasattr(self.engine, 'network_sync'):
                        self.engine.network_sync.set_current_session_id(None)
                        self.engine.network_sync.clear_pending_matches()
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
            # Сетевая ошибка — очередь НЕ очищается, сессия остаётся открытой для повтора
            return {"success": False, "error": f"Сбой соединения: {ex}"}

    def check_active_session(self):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            return {"success": False, "active_session": None, "manual_conflict": False}
        api_url = f"{_get_api_base_url()}/api/tracker/pulse"
        try:
            req = urllib.request.Request(api_url, headers={"x-sync-token": token})
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp_data = json.loads(resp.read().decode())
                if resp_data.get("success"):
                    active_sess = resp_data.get("active_session")
                    if self.engine and hasattr(self.engine, 'network_sync'):
                        sess_id = active_sess.get("session_id") if active_sess else None
                        self.engine.network_sync.set_current_session_id(sess_id)
                        self.engine.network_sync.set_manual_conflict(resp_data.get("manual_conflict", False))
                    return {
                        "success": True,
                        "active_session": active_sess,
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
            base_url = _get_api_base_url()
            if not is_url_allowed(url, base_url):
                log.warning(f"[SECURITY:URL_REJECTED] Отклонен переход по внешнему URL: {url}")
                return {"success": False, "error": "URL не разрешён политикой безопасности"}
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

    def send_report(self, message="", include_diagnostics=True):
        config = config_manager.load_config()
        token = config.get("sync_token", "").strip()
        if not token:
            return {"success": False, "error": "Привяжите токен в настройках перед отправкой отчета"}

        api_url = f"{_get_api_base_url()}/api/tracker/report"
        if include_diagnostics:
            diagnostics = self._get_system_diagnostics()
            raw_logs = logger.get_recent_logs()
            if len(raw_logs) > 500000:
                raw_logs = raw_logs[-500000:]
            recent_logs = diagnostics + raw_logs
        else:
            recent_logs = ""

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
            base_url = _get_api_base_url()
            api_url = f"{base_url}/api/tracker/version"
            req = urllib.request.Request(api_url, headers={
                "User-Agent": f"KD-Connect/{APP_VERSION}",
                "Accept": "application/json",
                "Cache-Control": "no-cache"
            })
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                latest_version = str(data.get("version") or "").strip()
                if not latest_version:
                    return {"update_available": False}

                download_url = _resolve_download_url(data.get("download_url"), base_url)
                installer_url = _resolve_download_url(data.get("installer_url") or data.get("download_url"), base_url)
                sha256_setup = str(data.get("sha256_setup") or "").strip()
                sha256_exe = str(data.get("sha256_exe") or "").strip()
                is_newer = _is_newer_version(latest_version, APP_VERSION)
                if is_newer:
                    log.info(f"[UPDATE] Доступна новая версия {latest_version} (текущая {APP_VERSION})")
                return {
                    "update_available": is_newer,
                    "current_version": APP_VERSION,
                    "latest_version": latest_version,
                    "release_name": str(data.get("release_name") or latest_version),
                    "download_url": download_url,
                    "installer_url": installer_url,
                    "sha256_setup": sha256_setup,
                    "sha256_exe": sha256_exe
                }
        except Exception as e:
            log.warning(f"[UPDATE:CHECK_ERR] {e}")
            return {"update_available": False, "error": str(e)}

    def apply_update(self, download_url=None, installer_url=None, sha256_setup=None):
        in_active_session = False
        if self.engine:
            sm = getattr(self.engine, 'state_machine', None)
            if sm and bool(getattr(sm, 'session_active', False)):
                in_active_session = True
            ns = getattr(self.engine, 'network_sync', None)
            if ns:
                if bool(getattr(ns, 'current_session_id', None)):
                    in_active_session = True
                if bool(getattr(ns, 'manual_conflict', False)):
                    in_active_session = True
            if bool(getattr(self.engine, 'session_active', False)):
                in_active_session = True
        if in_active_session:
            log.warning("[UPDATE:BLOCKED] Попытка обновления во время активного матча или сессии")
            return {"success": False, "error": "Нельзя обновлять приложение во время активного матча или сессии"}

        target_url = installer_url or download_url
        if not target_url:
            return {"success": False, "error": "URL для загрузки обновления не указан"}
        resolved_url = _resolve_download_url(target_url, _get_api_base_url())

        try:
            temp_dir = os.environ.get('TEMP', os.path.dirname(sys.executable))
            temp_setup = os.path.join(temp_dir, f"KD-Connect-Setup-{int(time.time())}.exe")

            log.info(f"[UPDATE] Загрузка установщика обновления с {resolved_url}")
            req = urllib.request.Request(resolved_url, headers={"User-Agent": f"KD-Connect/{APP_VERSION}"})
            hasher = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=45) as resp, open(temp_setup, 'wb') as out_f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    out_f.write(chunk)

            if not os.path.exists(temp_setup) or os.path.getsize(temp_setup) < 100000:
                if os.path.exists(temp_setup):
                    try: os.remove(temp_setup)
                    except Exception: pass
                return {"success": False, "error": "Ошибка целостности скачанного файла"}

            # Проверка SHA-256 установщика (жёсткий отказ при несовпадении)
            actual_sha256 = hasher.hexdigest()
            if sha256_setup and sha256_setup.strip():
                if actual_sha256.lower() != sha256_setup.strip().lower():
                    try: os.remove(temp_setup)
                    except Exception: pass
                    log.error(f"[UPDATE:SHA256_MISMATCH] Не совпал хэш установщика! Ожидался: {sha256_setup}, получен: {actual_sha256}")
                    return {"success": False, "error": "Ошибка проверки контрольной суммы (SHA-256) обновления"}

            # Проверка исполняемого файла MZ
            with open(temp_setup, 'rb') as check_f:
                if check_f.read(2) != b'MZ':
                    try: os.remove(temp_setup)
                    except Exception: pass
                    return {"success": False, "error": "Скачанный файл не является исполняемым (.exe)"}

            current_exe = os.path.abspath(sys.executable)
            if not getattr(sys, 'frozen', False):
                log.info(f"[UPDATE:DEV_MODE] Установщик сохранен в {temp_setup}")
                return {"success": True, "downloaded_path": temp_setup, "dev_mode": True}

            # Отвязанный помощник cmd (пауза 2 с → установщик /SILENT → start exe)
            bat_path = os.path.join(temp_dir, f"kd_update_{int(time.time())}.bat")
            bat_content = (
                "@echo off\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                f'"{temp_setup}" /SILENT /SUPPRESSMSGBOXES /NORESTART\r\n'
                f'start "" "{current_exe}"\r\n'
                '(goto) 2>nul & del "%~f0"\r\n'
            )
            with open(bat_path, "w", encoding="cp866") as bf:
                bf.write(bat_content)

            import subprocess
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            if hasattr(subprocess, 'DETACHED_PROCESS'):
                flags |= subprocess.DETACHED_PROCESS

            subprocess.Popen(
                ["cmd.exe", "/c", bat_path],
                creationflags=flags,
                close_fds=True
            )

            if webview.windows:
                try: webview.windows[0].destroy()
                except Exception: pass
            os._exit(0)
        except Exception as e:
            log.error(f"[UPDATE:APPLY_ERR] {e}")
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
    setup_crash_handlers()

    # 1. Проверка наличия WebView2 Runtime (п. 29)
    if not is_webview2_available():
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "Для работы KD Connect требуется компонент Microsoft Edge WebView2 Runtime.\n\n"
                "Пожалуйста, установите WebView2 с официального сайта Microsoft:\n"
                "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
                "После установки перезапустите приложение.",
                "KD Connect — Требуется WebView2",
                0x10 | 0x0
            )
        sys.exit(1)

    # 2. Одиночный запуск приложения через мьютекс Local\KDConnectSingleInstanceMutex (п. 17)
    _app_mutex = None
    if sys.platform == 'win32':
        try:
            import ctypes
            ERROR_ALREADY_EXISTS = 183
            kernel32 = ctypes.windll.kernel32
            _app_mutex = kernel32.CreateMutexW(None, False, "Local\\KDConnectSingleInstanceMutex")
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                ctypes.windll.user32.MessageBoxW(0, "Программа KD Connect уже запущена!", "KD Connect", 0x40 | 0x0)
                sys.exit(0)
        except Exception:
            pass
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
            background_color='#09090b',
            hidden=False
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

        import threading
        def _deferred_taskbar_setup():
            time.sleep(1.0)
            _enable_taskbar_minimize("KD Connect")
        threading.Thread(target=_deferred_taskbar_setup, daemon=True).start()

        def _setup_native_splash():
            if sys.platform != 'win32':
                return
            try:
                from System.Windows.Forms import ImageLayout, MethodInvoker
                from System.Drawing import Image
                form = window.native
                splash_path = get_asset_path('native_splash.png')
                if not os.path.exists(splash_path):
                    exe_dir = os.path.dirname(sys.executable)
                    alt_splash = os.path.join(exe_dir, 'native_splash.png')
                    if os.path.exists(alt_splash):
                        splash_path = alt_splash
                if os.path.exists(splash_path):
                    form.BackgroundImage = Image.FromFile(splash_path)
                    form.BackgroundImageLayout = ImageLayout.Stretch
                
                if hasattr(form, 'Controls') and form.Controls.Count > 0:
                    wv_ctrl = form.Controls[0]
                    wv_ctrl.Visible = False

                    def _safety_reveal():
                        time.sleep(1.8)
                        try:
                            form.Invoke(MethodInvoker(lambda: setattr(wv_ctrl, 'Visible', True)))
                        except Exception:
                            pass
                    threading.Thread(target=_safety_reveal, daemon=True).start()
            except Exception as ex:
                log.warning(f"Не удалось инициализировать нативный splash: {ex}")

        window.events.shown += _setup_native_splash

        webview.start(debug=False, icon=icon_path if os.path.exists(icon_path) else None)
        engine.stop()
    except Exception as e:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        _write_crash_record(f"MAIN CRASH: {ts}", traceback.format_exc())
        log.critical(f"Фатальный сбой в main(): {e}")
