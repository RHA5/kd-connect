import os
import json

APP_DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'KDConnect')
CONFIG_FILE = os.path.join(APP_DATA_DIR, 'config.json')
RECOVERY_FILE = os.path.join(APP_DATA_DIR, 'match_recovery.json')

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
        except Exception:
            pass

def load_config():
    ensure_app_data_dir()
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(data)
            return merged
    except Exception:
        return DEFAULT_CONFIG.copy()

def save_config(config_dict):
    ensure_app_data_dir()
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
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
