import os
import json

APP_DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'KDConnect')
CONFIG_FILE = os.path.join(APP_DATA_DIR, 'config.json')

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
