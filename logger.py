import os
import sys
import shutil
import logging
from logging.handlers import RotatingFileHandler

APP_DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'KDConnect')
LOG_FILE = os.path.join(APP_DATA_DIR, 'tracker.log')

# Убеждаемся, что директория существует
if not os.path.exists(APP_DATA_DIR):
    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
    except Exception:
        pass

# Настройка кольцевого буфера логов (до 3 МБ, 3 ротации)
_formatter = logging.Formatter(
    fmt='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

_root_logger = logging.getLogger("KDConnect")
_root_logger.setLevel(logging.INFO)

if not _root_logger.handlers:
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=3 * 1024 * 1024,
            backupCount=3,
            encoding='utf-8'
        )
        file_handler.setFormatter(_formatter)
        _root_logger.addHandler(file_handler)
    except Exception:
        pass

    if sys.stdout is not None:
        try:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(_formatter)
            _root_logger.addHandler(console_handler)
        except Exception:
            pass


def get_logger(name="KDConnect"):
    return logging.getLogger(f"KDConnect.{name}") if name != "KDConnect" else _root_logger


def get_recent_logs(max_bytes=150 * 1024):
    """Возвращает хвост логов (до 150 КБ) для отправки в тикет."""
    if not os.path.exists(LOG_FILE):
        return "[Лог пуст: сессий еще не зафиксировано]"
    try:
        file_size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            if file_size > max_bytes:
                f.seek(file_size - max_bytes)
                # Пропускаем возможную обрезку первой строки
                f.readline()
            return f.read()
    except Exception as e:
        return f"[Ошибка чтения tracker.log: {e}]"


def export_log(dest_path=None):
    """Экспортирует файл tracker.log на Рабочий стол пользователя."""
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write("KD Connect Log Initialized\n")

    if not dest_path:
        desktop = os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'Desktop')
        if not os.path.exists(desktop):
            desktop = os.path.expanduser('~')
        dest_path = os.path.join(desktop, 'KDConnect_tracker.log')

    shutil.copy2(LOG_FILE, dest_path)
    return dest_path
