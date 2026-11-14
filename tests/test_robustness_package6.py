import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config_manager
from log_tailer import LogTailer
from hunt_detector import get_fixed_drives, get_all_steam_libraries
from main import setup_crash_handlers, CRASH_LOG_PATH


class TestConfigManagerRobustness(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.orig_cfg = config_manager.CONFIG_FILE
        self.orig_bak = config_manager.CONFIG_BAK_FILE
        self.orig_app_dir = config_manager.APP_DATA_DIR
        
        config_manager.APP_DATA_DIR = self.test_dir.name
        config_manager.CONFIG_FILE = os.path.join(self.test_dir.name, "config.json")
        config_manager.CONFIG_BAK_FILE = os.path.join(self.test_dir.name, "config.json.bak")

    def tearDown(self):
        config_manager.CONFIG_FILE = self.orig_cfg
        config_manager.CONFIG_BAK_FILE = self.orig_bak
        config_manager.APP_DATA_DIR = self.orig_app_dir
        self.test_dir.cleanup()

    def test_atomic_save_and_backup(self):
        # Initial save
        config_manager.save_config({"sync_token": "kdm_initial_123"})
        self.assertTrue(os.path.exists(config_manager.CONFIG_FILE))
        self.assertFalse(os.path.exists(config_manager.CONFIG_BAK_FILE))

        # Second save creates .bak
        config_manager.save_config({"sync_token": "kdm_updated_456"})
        self.assertTrue(os.path.exists(config_manager.CONFIG_BAK_FILE))

        # Verify .bak has initial value and current has updated
        with open(config_manager.CONFIG_BAK_FILE, "r", encoding="utf-8") as f:
            bak_data = json.load(f)
            self.assertEqual(bak_data.get("sync_token"), "kdm_initial_123")

        with open(config_manager.CONFIG_FILE, "r", encoding="utf-8") as f:
            cur_data = json.load(f)
            self.assertEqual(cur_data.get("sync_token"), "kdm_updated_456")

    def test_corrupt_json_fallback_to_bak(self):
        # Save valid config
        config_manager.save_config({"sync_token": "valid_token", "custom_hunt_path": "C:\\Games\\Hunt"})
        self.assertTrue(os.path.exists(config_manager.CONFIG_FILE))

        # Trigger second save so .bak exists
        config_manager.update_config_value("sync_token", "valid_token_2")
        self.assertTrue(os.path.exists(config_manager.CONFIG_BAK_FILE))

        # Corrupt the main config.json
        with open(config_manager.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{corrupt json content missing quotes and closing brace")

        # load_config should recover from .bak
        data = config_manager.load_config()
        self.assertEqual(data.get("sync_token"), "valid_token")
        self.assertEqual(data.get("custom_hunt_path"), "C:\\Games\\Hunt")

        # Main config should have been restored
        with open(config_manager.CONFIG_FILE, "r", encoding="utf-8") as f:
            recovered_data = json.load(f)
            self.assertEqual(recovered_data.get("sync_token"), "valid_token")


class TestLogTailerRobustness(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.log_file = os.path.join(self.test_dir.name, "test.log")
        with open(self.log_file, "wb") as f:
            f.write(b"")

    def tearDown(self):
        self.test_dir.cleanup()

    def test_binary_reading_split_chunks(self):
        tailer = LogTailer(self.log_file, start_at_end=False)

        # Write first partial line without newline
        with open(self.log_file, "ab") as f:
            f.write(b"Line 1 part A ")

        lines = tailer.read_new_lines()
        self.assertEqual(len(lines), 0)
        self.assertEqual(tailer._remainder_bytes, b"Line 1 part A ")

        # Write the rest of line 1 with newline and full line 2
        with open(self.log_file, "ab") as f:
            f.write(b"part B complete\nLine 2 full\n")

        lines = tailer.read_new_lines()
        self.assertEqual(lines, ["Line 1 part A part B complete", "Line 2 full"])
        self.assertEqual(tailer._remainder_bytes, b"")

    def test_utf8_replace_on_invalid_bytes(self):
        tailer = LogTailer(self.log_file, start_at_end=False)

        # Invalid utf-8 byte sequence \xff\xfe
        with open(self.log_file, "ab") as f:
            f.write(b"Valid start \xff\xfe valid end\n")

        lines = tailer.read_new_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue("Valid start" in lines[0])
        self.assertTrue("valid end" in lines[0])
        # Replacement character \ufffd should be present
        self.assertIn("\ufffd", lines[0])


class TestHuntDetectorDrives(unittest.TestCase):
    def test_get_fixed_drives_real_machine(self):
        drives = get_fixed_drives()
        self.assertIsInstance(drives, list)
        self.assertTrue(len(drives) > 0)
        # On Windows C is always a fixed drive
        self.assertTrue(any("C" == d.upper() for d in drives))

    @patch("ctypes.windll.kernel32.GetLogicalDrives")
    @patch("ctypes.windll.kernel32.GetDriveTypeW")
    def test_get_fixed_drives_filtering(self, mock_get_type, mock_get_drives):
        # Bitmask for A(bit 0), C(bit 2), D(bit 3), E(bit 4) = 1 | 4 | 8 | 16 = 29
        mock_get_drives.return_value = 29

        def drive_type_side_effect(drive_str):
            if drive_str.startswith("A:"): return 2  # DRIVE_REMOVABLE
            if drive_str.startswith("C:"): return 3  # DRIVE_FIXED
            if drive_str.startswith("D:"): return 4  # DRIVE_REMOTE
            if drive_str.startswith("E:"): return 5  # DRIVE_CDROM
            return 1
        mock_get_type.side_effect = drive_type_side_effect

        drives = get_fixed_drives()
        self.assertEqual(drives, ["C"])


class TestCrashHandlerSetup(unittest.TestCase):
    def test_setup_crash_handlers(self):
        orig_sys_hook = sys.excepthook
        orig_thread_hook = getattr(sys.modules.get("threading"), "excepthook", None)
        try:
            setup_crash_handlers()
            self.assertNotEqual(sys.excepthook, orig_sys_hook)
        finally:
            sys.excepthook = orig_sys_hook
            if orig_thread_hook:
                import threading
                threading.excepthook = orig_thread_hook


if __name__ == "__main__":
    unittest.main()
