import unittest
import os
import re

class TestPreviewHtml(unittest.TestCase):
    def setUp(self):
        html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "preview.html"))
        with open(html_path, "r", encoding="utf-8") as f:
            self.html = f.read()

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
        self.assertIn('id="modal-report"', self.html)
        self.assertIn('id="report-include-diag"', self.html)

        # Banners
        self.assertIn('id="nvidia-hint-banner"', self.html)
        self.assertIn('id="active-manual-conflict-box"', self.html)
        self.assertIn('id="verify-server-error"', self.html)

    def test_functions_exist(self):
        self.assertIn("openConfirmCombatModal", self.html)
        self.assertIn("openConfirmPendingModal", self.html)
        self.assertIn("openReportModal", self.html)
        self.assertIn("submitErrorReport", self.html)
        self.assertIn("startMatchTimer", self.html)
        self.assertIn("stopMatchTimer", self.html)
        self.assertIn("match_started_at", self.html)

if __name__ == "__main__":
    unittest.main()
