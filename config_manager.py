import os
import json
import logging
import shutil

log = logging.getLogger("KDConnect.Config")

APP_DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'KDConnect')
CONFIG_FILE = os.path.join(APP_DATA_DIR, 'config.json')
CONFIG_BAK_FILE = os.path.join(APP_DATA_DIR, 'config.json.bak')
RECOVERY_FILE = os.path.join(APP_DATA_DIR, 'match_recovery.json')
PENDING_MATCHES_FILE = os.path.join(APP_DATA_DIR, 'pending_matches.json')

DEFAULT_CONFIG = {
    "sync_token": "",
    "custom_hunt_path": "",
    "api_base_url": "https://kdmetrika.ru",
    "hide_disclaimer": False,
    "onboarding_completed": False
}

def ensure_app_data_dir():
    if not os.path.exists(APP_DATA_DIR):
        try:
            os.makedirs(APP_DATA_DIR, exist_ok=True)
        except Exception as e:
            log.warning(f"Не удалось создать директорию AppData: {e}")

def load_config():
    ensure_app_data_dir()
    if not os.path.exists(CONFIG_FILE):
        if os.path.exists(CONFIG_BAK_FILE):
            log.warning("[CONFIG:RESTORE] config.json отсутствует, попытка загрузить из config.json.bak")
            try:
                with open(CONFIG_BAK_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged = DEFAULT_CONFIG.copy()
                    merged.update(data)
                    save_config(merged)
                    return merged
            except Exception as e:
                log.error(f"[CONFIG:FAIL] Не удалось восстановить из .bak: {e}")
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            merged = DEFAULT_CONFIG.copy()
            merged.update(data)
            return merged
        raise ValueError("config.json содержит невалидный JSON объект")
    except Exception as e:
        log.warning(f"[CONFIG:CORRUPT] config.json поврежден ({e}), попытка восстановить из .bak")
        if os.path.exists(CONFIG_BAK_FILE):
            try:
                with open(CONFIG_BAK_FILE, 'r', encoding='utf-8') as f:
                    bak_data = json.load(f)
                if isinstance(bak_data, dict):
                    merged = DEFAULT_CONFIG.copy()
                    merged.update(bak_data)
                    log.info("[CONFIG:RESTORE] Конфигурация успешно восстановлена из config.json.bak")
                    save_config(merged)
                    return merged
            except Exception as bak_err:
                log.error(f"[CONFIG:FAIL] Ошибка чтения config.json.bak: {bak_err}")

        log.error("[CONFIG:FAIL] Не удалось прочитать config.json и .bak, возврат дефолтной конфигурации")
        return DEFAULT_CONFIG.copy()

def save_config(config_dict):
    ensure_app_data_dir()
    try:
        # Если основной файл уже существует и валиден, обновляем .bak перед перезаписью
        if os.path.exists(CONFIG_FILE):
            try:
                shutil.copy2(CONFIG_FILE, CONFIG_BAK_FILE)
            except Exception as bak_ex:
                log.warning(f"[CONFIG:BAK_ERR] Не удалось обновить .bak: {bak_ex}")

        tmp_path = CONFIG_FILE + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_FILE)
        return True
    except Exception as e:
        log.error(f"[CONFIG:SAVE_ERR] Ошибка атомарного сохранения config.json: {e}")
        return False

def update_config_value(key, value):
    config = load_config()
    config[key] = value
    save_config(config)
    return config

def save_recovery_state(data):
    ensure_app_data_dir()
    try:
        tmp_path = RECOVERY_FILE + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if os.path.exists(RECOVERY_FILE):
            os.replace(tmp_path, RECOVERY_FILE)
        else:
            os.rename(tmp_path, RECOVERY_FILE)
        return True
    except Exception:
        return False

def load_recovery_state():
    ensure_app_data_dir()
    if not os.path.exists(RECOVERY_FILE):
        return None
    try:
        with open(RECOVERY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict) and data.get("match_id"):
                return data
    except Exception:
        pass
    return None

def clear_recovery_state():
    try:
        if os.path.exists(RECOVERY_FILE):
            os.remove(RECOVERY_FILE)
    except Exception:
        pass

def save_pending_matches(matches):
    ensure_app_data_dir()
    try:
        tmp_path = PENDING_MATCHES_FILE + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(matches, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, PENDING_MATCHES_FILE)
        return True
    except Exception:
        return False

def load_pending_matches():
    ensure_app_data_dir()
    if not os.path.exists(PENDING_MATCHES_FILE):
        return []
    try:
        with open(PENDING_MATCHES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

def clear_pending_matches_file():
    try:
        if os.path.exists(PENDING_MATCHES_FILE):
            os.remove(PENDING_MATCHES_FILE)
    except Exception:
        pass

