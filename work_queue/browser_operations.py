"""Run explicitly in a Playwright container against a disposable UI preview."""

import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


class OperationsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_url = os.environ["VIBEARR_UI_PREVIEW_URL"].rstrip("/")
        cls.artifacts = Path(
            os.environ.get("VIBEARR_UI_ARTIFACTS", "/tmp/ui-screenshots")
        )
        cls.artifacts.mkdir(parents=True, exist_ok=True)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 1100}
        )
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def visit(self, path):
        response = self.page.goto(self.base_url + path)
        self.assertEqual(response.status, 200)

    def screenshot(self, name):
        self.page.screenshot(path=str(self.artifacts / name), full_page=True)

    def test_queue_status_navigation_preserves_filters_and_clear_keeps_status(self):
        self.visit("/?q=queue&priority=high&status=queued")
        self.page.get_by_role("navigation", name="Filter by status").get_by_role(
            "link", name="Failed"
        ).click()
        query = parse_qs(urlparse(self.page.url).query)
        self.assertEqual(
            query, {"q": ["queue"], "priority": ["high"], "status": ["failed"]}
        )
        expect(self.page.locator("#status-filter")).to_have_value("failed")
        self.page.locator(".filter-summary").get_by_role(
            "link", name="Clear filters"
        ).click()
        self.assertEqual(
            parse_qs(urlparse(self.page.url).query), {"status": ["failed"]}
        )
        expect(self.page.locator(".queue-item")).to_have_count(1)

    def test_configuration_queue_activity_and_history_navigation(self):
        self.visit("/system/")
        self.page.get_by_role("link", name="Configure processing", exact=True).click()
        expect(
            self.page.get_by_role("heading", name="Settings", exact=True)
        ).to_be_visible()
        self.page.get_by_role("navigation", name="Main navigation").get_by_role(
            "link", name="Queue", exact=True
        ).click()
        self.page.get_by_role(
            "link", name="View latest attempt and controls"
        ).first.click()
        expect(
            self.page.get_by_role("button", name="Request cancellation")
        ).to_be_visible()
        expect(self.page.get_by_role("button", name="Stop and retry")).to_be_visible()
        self.page.get_by_role("navigation", name="Main navigation").get_by_role(
            "link", name="Activity", exact=True
        ).click()
        expect(
            self.page.get_by_text("Waiting for required CI checks", exact=False)
        ).to_be_visible()
        self.screenshot("activity-desktop.png")
        self.page.get_by_role("navigation", name="Main navigation").get_by_role(
            "link", name="History", exact=True
        ).click()
        self.page.locator("#run-state").select_option("completed")
        self.page.get_by_role("button", name="Filter", exact=True).click()
        expect(self.page.get_by_text("succeeded", exact=True)).to_be_visible()
        expect(
            self.page.get_by_role("link", name="View pull request")
        ).to_have_attribute("href", "https://github.com/example/ui/pull/7")
        self.screenshot("history-desktop.png")

    def test_queue_desktop_and_mobile_fit_viewport(self):
        self.visit("/")
        self.screenshot("queue-desktop.png")
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.reload()
        expect(
            self.page.get_by_role("navigation", name="Main navigation")
        ).to_be_visible()
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"), 390
        )
        self.screenshot("queue-mobile.png")

    def test_system_mobile_dark_theme_and_unknown_state(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.visit("/system/?theme=dark")
        expect(self.page.locator("html")).to_have_attribute("data-theme", "dark")
        expect(
            self.page.get_by_text(
                "Free space and worker health are unknown", exact=False
            )
        ).to_be_visible()
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"), 390
        )
        self.screenshot("system-mobile-dark.png")

    def test_operator_forms_send_csrf_and_task_text_is_escaped(self):
        self.visit("/activity/")
        expect(self.page.locator(".run-card script")).to_have_count(0)
        self.page.get_by_role("link", name="Details, evidence and controls").click()
        form = self.page.locator('form[action$="/cancel/"]')
        expect(form.locator('input[name="csrfmiddlewaretoken"]')).not_to_have_value("")
        self.assertEqual(form.get_attribute("method"), "post")
        self.screenshot("run-detail-desktop.png")


if __name__ == "__main__":
    unittest.main()
