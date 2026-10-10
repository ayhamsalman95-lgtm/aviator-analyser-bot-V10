"""Regression checks for direct-launch validation and separate-window capture ordering."""
import inspect
import unittest

from aviator.collector import Collector
from aviator.launch import launch_delay_seconds, redact_sensitive_text, validate_launch_url
from aviator.netlog import safe_url


class LaunchValidationTests(unittest.TestCase):
    def test_accepts_https_spribe_launch_url(self):
        url = "https://launch.spribegaming.com/aviator?user=123&token=secret&lang=en"
        self.assertEqual(validate_launch_url(url), url)

    def test_rejects_wrong_scheme_or_host(self):
        for url in (
            "http://launch.spribegaming.com/aviator?token=secret",
            "https://spribegaming.com/aviator",
            "https://launch.spribegaming.com.attacker.invalid/aviator",
            "https://user:pass@launch.spribegaming.com/aviator",
            "https://launch.spribegaming.com/aviator#fragment",
            "",
        ):
            with self.subTest(url=url.split("?", 1)[0]):
                with self.assertRaises(ValueError):
                    validate_launch_url(url)

    def test_delay_defaults_to_twenty_seconds_and_is_configurable(self):
        self.assertEqual(launch_delay_seconds(None), 20.0)
        self.assertEqual(launch_delay_seconds(""), 20.0)
        self.assertEqual(launch_delay_seconds("20"), 20.0)
        self.assertEqual(launch_delay_seconds("0.25"), 0.25)

    def test_delay_rejects_invalid_or_unbounded_values(self):
        for value in ("not-a-number", "-1", "301", "nan", "inf"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    launch_delay_seconds(value)

    def test_secret_values_are_redacted_from_urls_and_errors(self):
        raw = "https://launch.spribegaming.com/aviator?user=12345&token=TOPSECRET&lang=en"
        self.assertNotIn("12345", safe_url(raw))
        self.assertNotIn("TOPSECRET", safe_url(raw))
        diagnostic = redact_sensitive_text(f"navigation failed for {raw}")
        self.assertNotIn("12345", diagnostic)
        self.assertNotIn("TOPSECRET", diagnostic)
        self.assertIn("redacted", diagnostic.lower())


class SeparateWindowWorkflowRegressionTests(unittest.TestCase):
    def test_default_workflow_waits_for_manual_spribe_window(self):
        source = inspect.getsource(Collector.run_session)
        self.assertIn('raw_launch_url = os.environ.get("AVIATOR_GAME_URL", "").strip()', source)
        self.assertIn("await operator_page.goto(site_home_url", source)
        self.assertIn("await operator_page.goto(operator_url", source)
        self.assertIn('context.on("page", on_new_page)', source)
        self.assertIn("Open Spribe Aviator manually in a SEPARATE window", source)
        self.assertIn("candidate is operator_page", source)
        self.assertIn("manual_spribe_window_selected", source)
        self.assertIn("waiting_for_manual_spribe_window", source)
        self.assertIn("capture_page = None", source)

    def test_optional_direct_launch_keeps_listeners_before_navigation(self):
        source = inspect.getsource(Collector.run_session)
        steps = [
            "await asyncio.sleep(launch_delay)",
            '"Target.createTarget"',
            "attach(capture_page)",
            "await capture_page.goto(launch_url",
        ]
        positions = [source.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))

    def test_direct_capture_window_has_its_listeners_before_navigation(self):
        source = inspect.getsource(Collector.run_session)
        self.assertLess(source.index("attach(capture_page)"),
                        source.index("await capture_page.goto(launch_url"))
        self.assertIn('page.on("websocket", on_ws)', source)
        self.assertIn('page.on("request", on_request)', source)
        self.assertIn('page.on("response", on_response)', source)
        self.assertIn('page.on("framenavigated", on_frame_navigated)', source)

    def test_frame_scan_never_reassigns_capture_page_or_operator_page(self):
        source = inspect.getsource(Collector.run_session)
        self.assertNotIn("page = pg", source)
        self.assertIn("refreshed = await self._find_live_game_frame(capture_page)", source)

    def test_cdp_cleanup_does_not_close_operator_tab(self):
        source = inspect.getsource(Collector.run_session)
        self.assertIn("capture_page is not operator_page", source)
        self.assertNotIn("await operator_page.close()", source)


if __name__ == "__main__":
    unittest.main()
