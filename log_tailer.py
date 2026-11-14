import os
import time

class LogTailer:
    """
    Пассивный потокобезопасный читатель логов Windows (FileShare.ReadWrite).
    Не блокирует запись процесса игры или драйвера.
    Поддерживает сверку 64-байтового хвоста для мгновенного определения перезапуска игры при крашах.
    """
    def __init__(self, filepath, start_at_end=True):
        self.filepath = filepath
        self.last_pos = 0
        self.last_tail_bytes = b""
        self._remainder_bytes = b""
        if filepath and os.path.exists(filepath):
            if start_at_end:
                try:
                    self.last_pos = os.path.getsize(filepath)
                    self._update_tail_bytes()
                except Exception:
                    self.last_pos = 0

    def set_filepath(self, filepath, start_at_end=True):
        self.filepath = filepath
        self.last_pos = 0
        self.last_tail_bytes = b""
        self._remainder_bytes = b""
        if filepath and os.path.exists(filepath):
            if start_at_end:
                try:
                    self.last_pos = os.path.getsize(filepath)
                    self._update_tail_bytes()
                except Exception:
                    self.last_pos = 0

    def _update_tail_bytes(self):
        if not self.filepath or not os.path.exists(self.filepath) or self.last_pos <= 0:
            self.last_tail_bytes = b""
            return
        try:
            check_len = min(64, self.last_pos)
            with open(self.filepath, 'rb') as fb:
                fb.seek(self.last_pos - check_len)
                self.last_tail_bytes = fb.read(check_len)
        except Exception:
            self.last_tail_bytes = b""

    def read_new_lines(self):
        if not self.filepath or not os.path.exists(self.filepath):
            return []

        try:
            current_size = os.path.getsize(self.filepath)
        except Exception:
            return []

        # 1. Если файл уменьшился в размере — игра перезапустилась, сбрасываем указатель
        if current_size < self.last_pos:
            self.last_pos = 0
            self.last_tail_bytes = b""
            self._remainder_bytes = b""

        # 2. Сверка 64-байтового хвоста: если размер тот же или больше, но байты по старому смещению не совпадают
        elif self.last_pos > 0 and self.last_tail_bytes:
            try:
                check_len = len(self.last_tail_bytes)
                if current_size >= self.last_pos:
                    with open(self.filepath, 'rb') as fb:
                        fb.seek(self.last_pos - check_len)
                        actual_bytes = fb.read(check_len)
                        if actual_bytes != self.last_tail_bytes:
                            # Файл был перезаписан с нуля другим содержимым при перезапуске игры
                            self.last_pos = 0
                            self.last_tail_bytes = b""
                            self._remainder_bytes = b""
            except Exception:
                pass

        if current_size == self.last_pos:
            return []

        new_lines = []
        try:
            with open(self.filepath, 'rb') as f:
                f.seek(self.last_pos)
                raw_chunk = f.read()
                self.last_pos = f.tell()

            if raw_chunk:
                combined = self._remainder_bytes + raw_chunk
                parts = combined.split(b'\n')
                # Последний кусок — незавершённый остаток строки (до следующего перевода строки)
                self._remainder_bytes = parts[-1]
                # Обрабатываем все полные строки
                for raw_line in parts[:-1]:
                    if raw_line.endswith(b'\r'):
                        raw_line = raw_line[:-1]
                    try:
                        decoded = raw_line.decode('utf-8', errors='replace').strip()
                        if decoded:
                            new_lines.append(decoded)
                    except Exception:
                        pass

            self._update_tail_bytes()
        except Exception:
            pass

        return new_lines


class DynamicNvidiaLogTailer:
    """
    Отслеживает активный лог NVIDIA GfeSDK.
    Если игра перезапускается и появляется новый файл HuntGame_<PID>.log,
    автоматически переключается на свежий файл.
    """
    def __init__(self, detector_func, start_at_end=True):
        self.detector_func = detector_func
        self.current_filepath = None
        self.tailer = None
        self.start_at_end = start_at_end
        self.refresh_target()

    def refresh_target(self):
        res = self.detector_func()
        latest_file = res[0] if isinstance(res, (tuple, list)) else res
        if latest_file and latest_file != self.current_filepath:
            self.current_filepath = latest_file
            self.tailer = LogTailer(self.current_filepath, start_at_end=self.start_at_end)
            return True
        return False

    def read_new_lines(self):
        self.refresh_target()
        if not self.tailer:
            return []
        return self.tailer.read_new_lines()
