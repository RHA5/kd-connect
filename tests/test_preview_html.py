import unittest
import os
import re

class TestPreviewHtml(unittest.TestCase):
    def setUp(self):
        html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "preview.html"))
        with open(html_path, "r", encoding="utf-8") as f:
            self.html = f.read()

        main_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
        with open(main_path, "r", encoding="utf-8") as f:
            self.main_code = f.read()

    def test_csp_and_security(self):
        # CSP tag exists
        self.assertIn("Content-Security-Policy", self.html)
        self.assertIn("default-src 'self'", self.html)
        self.assertIn("img-src 'self' data:", self.html)
        self.assertIn("'unsafe-eval'", self.html)

        # External CDN removed
        self.assertNotIn("https://cdn.tailwindcss.com", self.html)
        self.assertNotIn("fonts.googleapis.com", self.html)
        self.assertNotIn("tailwind.config =", self.html)

    def test_dialogs_and_banners(self):
        # Dialogs
        self.assertIn('id="modal-confirm-combat"', self.html)
        self.assertIn('id="modal-confirm-pending"', self.html)
        self.assertIn('id="modal-verify-error"', self.html)
        self.assertNotIn('id="modal-pending"', self.html)
        self.assertNotIn('id="toggle-autostart"', self.html)
        self.assertIn('Полный сброс настроек', self.html)

        # Отчет полностью удален
        self.assertNotIn('id="modal-report"', self.html)
        self.assertNotIn('id="btn-open-report"', self.html)
        self.assertNotIn('id="report-include-diag"', self.html)
        self.assertNotIn('report', self.html.lower())

        # Banners
        self.assertNotIn('id="nvidia-hint-banner"', self.html)
        self.assertIn('id="active-manual-conflict-box"', self.html)
        self.assertIn('id="verify-server-error"', self.html)

    def test_functions_exist(self):
        self.assertIn("openConfirmCombatModal", self.html)
        self.assertIn("openConfirmPendingModal", self.html)
        self.assertIn("openVerifyErrorModal", self.html)
        self.assertIn("handleRetryVerifySubmit", self.html)
        self.assertIn("handleSaveWithoutVerify", self.html)
        self.assertNotIn("onAutoStartPending", self.html)
        self.assertNotIn("openReportModal", self.html)
        self.assertNotIn("submitErrorReport", self.html)
        self.assertNotIn("get_pending_matches", self.html)
        self.assertNotIn("handleToggleAutostart", self.html)
        self.assertNotIn("pendingMatchesState", self.html)
        self.assertIn("startWindowDrag", self.html)
        self.assertIn("startMatchTimer", self.html)
        self.assertIn("stopMatchTimer", self.html)
        self.assertIn("match_started_at", self.html)
        self.assertIn("handleEndSessionWebsite", self.html)

    def test_dialog_flag_propagation(self):
        # O1: Проверка передачи обоих флагов через модалки подтверждения
        # 1. Combat confirm передает forceDiscard и true
        self.assertTrue(re.search(
            r'handleConfirmCombatEnd\(\)\s*\{[\s\S]*?executeEndSession\([^,]+,\s*[^,]+,\s*pendingEndSessionParams\.forceDiscard,\s*true\)',
            self.html
        ))
        # 2. Retry pending передает false и forceInCombat
        self.assertTrue(re.search(
            r'handleRetryFlushPending\(\)\s*\{[\s\S]*?executeEndSession\([^,]+,\s*[^,]+,\s*false,\s*pendingEndSessionParams\.forceInCombat\)',
            self.html
        ))
        # 3. Discard pending передает true и forceInCombat
        self.assertTrue(re.search(
            r'handleConfirmDiscardPending\(\)\s*\{[\s\S]*?executeEndSession\([^,]+,\s*[^,]+,\s*true,\s*pendingEndSessionParams\.forceInCombat\)',
            self.html
        ))
        # 4. openConfirmCombatModal и openConfirmPendingModal принимают и сохраняют флаги
        self.assertIn("openConfirmCombatModal(adjKills, adjDeaths, forceDiscard = false)", self.html)
        self.assertIn("openConfirmPendingModal(adjKills, adjDeaths, pendingCount, forceInCombat = false)", self.html)

    def test_tailwind_classes_and_backdrop(self):
        # S2: backdrop-blur-xs заменен на backdrop-blur-sm
        self.assertNotIn("backdrop-blur-xs", self.html)
        self.assertIn("backdrop-blur-sm", self.html)

    def test_native_window_drag(self):
        # Header не должен содержать pywebview-drag-region
        header_match = re.search(r'<header[^>]*id="app-header"[^>]*>', self.html)
        self.assertIsNotNone(header_match)
        header_tag = header_match.group(0)
        self.assertNotIn("pywebview-drag-region", header_tag)
        self.assertIn('onmousedown="startWindowDrag(event)"', header_tag)

        # Функция startWindowDrag существует и фильтрует клики
        self.assertIn("function startWindowDrag(e)", self.html)
        self.assertIn("closest('button, a, input, textarea, select, label')", self.html)
        self.assertIn("window.pywebview.api.start_window_drag()", self.html)

        # В main.py (AppApi) есть нативный метод start_window_drag
        self.assertIn("def start_window_drag(self):", self.main_code)
        self.assertIn("ReleaseCapture", self.main_code)
        self.assertIn("SendMessageW", self.main_code)

    def test_match_timer_rounding(self):
        # startMatchTimer rounds input and difference with Math.floor
        self.assertIn("matchStartTimeUnix = Math.floor(startTimeOrElapsed);", self.html)
        self.assertIn("diffSec = Math.max(0, Math.floor(curNow - matchStartTimeUnix));", self.html)

        # stopMatchTimer rounds finalDur with Math.floor
        self.assertIn("const total = Math.max(0, Math.floor(Number(finalDur) || 0));", self.html)
        self.assertIn("const mm = Math.floor(total / 60).toString().padStart(2, '0');", self.html)
        self.assertIn("const ss = (total % 60).toString().padStart(2, '0');", self.html)

    def test_section_verify_typography(self):
        match = re.search(r'<div id="section-verify"[\s\S]*?<div id="settings-view"', self.html)
        self.assertIsNotNone(match)
        verify_html = match.group(0)
        self.assertNotIn('font-mono', verify_html)


if __name__ == "__main__":
    unittest.main()
