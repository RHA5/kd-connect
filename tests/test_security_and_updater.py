"""
Unit tests for Package 4 & 5 backend logic:
- URL allowlist in open_external_url
- Token error formatting
- Updater sha256 verification and session blocking
"""
import unittest
import os
import sys
import tempfile
import hashlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import main


class TestSecurityAndUpdater(unittest.TestCase):
    def test_url_allowlist(self):
        base_api = "https://kdmetrika.ru"
        # Allowed URLs
        self.assertTrue(main.is_url_allowed("https://kdmetrika.ru/dashboard", base_api))
        self.assertTrue(main.is_url_allowed("https://api.kdmetrika.ru/status", base_api))
        self.assertTrue(main.is_url_allowed("https://t.me/kdmetrika_support", base_api))
        self.assertTrue(main.is_url_allowed("http://localhost:3000/api", base_api))
        self.assertTrue(main.is_url_allowed("http://127.0.0.1:8000/test", base_api))
        self.assertTrue(main.is_url_allowed("https://localhost:3000", base_api))
        self.assertTrue(main.is_url_allowed("https://www.nvidia.com/ru-ru/software/nvidia-app/", base_api))
        self.assertTrue(main.is_url_allowed("https://nvidia.com/drivers", base_api))

        # Rejected URLs
        self.assertFalse(main.is_url_allowed("http://kdmetrika.ru/not-https", base_api))
        self.assertFalse(main.is_url_allowed("http://www.nvidia.com/insecure", base_api))
        self.assertFalse(main.is_url_allowed("https://evil.com/phishing", base_api))
        self.assertFalse(main.is_url_allowed("https://fake-nvidia.com", base_api))
        self.assertFalse(main.is_url_allowed("javascript:alert(1)", base_api))
        self.assertFalse(main.is_url_allowed("file:///C:/windows/system32/cmd.exe", base_api))
        self.assertFalse(main.is_url_allowed("https://not-kdmetrika.ru", base_api))

    def test_token_error_formatting(self):
        self.assertEqual(
            main._format_token_error("Недействительный токен синхронизации"),
            "Неверный токен синхронизации"
        )
        self.assertEqual(
            main._format_token_error("Токен не найден в базе"),
            "Неверный токен синхронизации"
        )
        self.assertEqual(
            main._format_token_error("Требуется роль бета-тестера (TESTER)"),
            "Доступ ограничен (требуется роль бета-тестера)"
        )
        self.assertEqual(
            main._format_token_error("Connection timeout", 500),
            "Сервер синхронизации временно недоступен"
        )
        self.assertEqual(
            main._format_token_error("Forbidden", 403),
            "Доступ ограничен или неверный токен"
        )

    def test_apply_update_blocks_during_session(self):
        api = main.AppApi()
        mock_engine = MagicMock()
        mock_engine.session_active = False
        mock_engine.state_machine.session_active = False
        mock_engine.network_sync.current_session_id = "test-session-123"
        mock_engine.network_sync.manual_conflict = False
        api.set_engine(mock_engine)

        with patch("urllib.request.urlopen") as mock_urlopen:
            res = api.apply_update("https://kdmetrika.ru/downloads/KD-Connect-Setup.exe")
            self.assertFalse(res.get("success"))
            self.assertIn("активного матча или сессии", res.get("error"))
            mock_urlopen.assert_not_called()

        # Также проверяем блокировку при активном бое в state_machine
        mock_engine.network_sync.current_session_id = None
        mock_engine.state_machine.session_active = True
        with patch("urllib.request.urlopen") as mock_urlopen:
            res2 = api.apply_update("https://kdmetrika.ru/downloads/KD-Connect-Setup.exe")
            self.assertFalse(res2.get("success"))
            mock_urlopen.assert_not_called()

        # И при manual_conflict
        mock_engine.state_machine.session_active = False
        mock_engine.network_sync.manual_conflict = True
        with patch("urllib.request.urlopen") as mock_urlopen:
            res3 = api.apply_update("https://kdmetrika.ru/downloads/KD-Connect-Setup.exe")
            self.assertFalse(res3.get("success"))
            mock_urlopen.assert_not_called()

    def test_apply_update_sha256_mismatch_fails(self):
        api = main.AppApi()
        mock_engine = MagicMock()
        mock_engine.session_active = False
        mock_engine.state_machine.session_active = False
        mock_engine.network_sync.current_session_id = None
        mock_engine.network_sync.manual_conflict = False
        api.set_engine(mock_engine)

        # Создаем временный файл-заглушку с MZ
        fake_content = b"MZ" + b"0" * 200000
        real_hash = hashlib.sha256(fake_content).hexdigest()
        bad_hash = "0" * 64

        class DummyResponse:
            def __init__(self, data):
                self._data = data
                self._pos = 0
            def read(self, n=None):
                if n is None or n < 0:
                    res = self._data[self._pos:]
                    self._pos = len(self._data)
                    return res
                res = self._data[self._pos:self._pos + n]
                self._pos += len(res)
                return res
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch("urllib.request.urlopen", return_value=DummyResponse(fake_content)):
            res = api.apply_update(
                download_url="https://kdmetrika.ru/downloads/KD-Connect-Setup.exe",
                sha256_setup=bad_hash
            )
            self.assertFalse(res.get("success"))
            self.assertIn("SHA-256", res.get("error"))

            # А с верным хэшем в dev mode должно быть success: True
            with patch("urllib.request.urlopen", return_value=DummyResponse(fake_content)):
                res_ok = api.apply_update(
                    download_url="https://kdmetrika.ru/downloads/KD-Connect-Setup.exe",
                    sha256_setup=real_hash
                )
                self.assertTrue(res_ok.get("success"))
                self.assertTrue(res_ok.get("dev_mode"))
                if res_ok.get("downloaded_path") and os.path.exists(res_ok.get("downloaded_path")):
                    try:
                        os.remove(res_ok.get("downloaded_path"))
                    except Exception:
                        pass


if __name__ == "__main__":
    unittest.main()
