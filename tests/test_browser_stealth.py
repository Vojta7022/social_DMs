from __future__ import annotations

import unittest

from src.browser.stealth import _build_context_options, _build_launch_options


class BrowserStealthOptionTests(unittest.TestCase):
    def test_headed_launch_uses_real_chrome_without_enable_automation(self) -> None:
        options = _build_launch_options(headless=False, proxy_url=None)
        self.assertEqual(options["channel"], "chrome")
        self.assertEqual(options["ignore_default_args"], ["--enable-automation"])
        self.assertNotIn("--disable-blink-features=AutomationControlled", options["args"])
        self.assertNotIn("--disable-infobars", options["args"])
        self.assertNotIn("--no-sandbox", options["args"])
        self.assertNotIn("--disable-setuid-sandbox", options["args"])

    def test_headless_launch_keeps_stealth_flags(self) -> None:
        options = _build_launch_options(headless=True, proxy_url="http://proxy.local:8080")
        self.assertNotIn("channel", options)
        self.assertEqual(options["proxy"], {"server": "http://proxy.local:8080"})
        self.assertIn("--disable-blink-features=AutomationControlled", options["args"])

    def test_headed_context_uses_browser_defaults(self) -> None:
        options = _build_context_options(headless=False)
        self.assertNotIn("user_agent", options)
        self.assertNotIn("timezone_id", options)
        self.assertEqual(options["viewport"], {"width": 1366, "height": 768})

    def test_headless_context_keeps_explicit_fingerprint(self) -> None:
        options = _build_context_options(headless=True)
        self.assertIn("user_agent", options)
        self.assertEqual(options["timezone_id"], "America/New_York")


if __name__ == "__main__":
    unittest.main()
