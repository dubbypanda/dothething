"""Persistent Camoufox identity checks with temporary profiles only.

DTT_TEST_BROWSER=1 adds installed-library and local headless/headed checks.
No test uses a saved account or visits a remote page.
"""
import copy
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import types
import threading
import unittest
from unittest.mock import Mock, patch

from test_browser_sessions import load_browser_code


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dtt-fingerprint-unit-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.code = load_browser_code()
        self.Browser = self.code["Browser"]
        self.browsers = []
        self.addon_path = "/runtime/addons/UBO"
        self.executable = "/runtime/camoufox"
        self.generate = Mock(side_effect=self.generate_options)
        self.utils = types.ModuleType("camoufox.utils")
        self.utils.launch_options = self.generate
        self.utils.get_target_os = lambda _config: "mac"
        self.utils.get_env_vars = lambda config, _os: {"CAMOU_CONFIG_1": json.dumps(config)}
        self.utils.launch_path = lambda: self.executable
        self.utils.validate_config = Mock()
        self.properties = self.root / "properties.json"
        self.properties.write_text(json.dumps([{"property": key} for key in (
            "navigator.userAgent", "navigator.platform", "navigator.hardwareConcurrency",
            "screen.width", "screen.height", "fonts", "voices",
            "fonts:spacing_seed", "audio:seed", "canvas:seed", "webGl:vendor", "webGl:renderer",
        )]))
        self.utils.get_path = lambda _name: str(self.properties)
        self.addons = types.ModuleType("camoufox.addons")
        self.addons.add_default_addons = lambda paths: paths.append(self.addon_path)
        self.addons.confirm_paths = Mock()
        package = types.ModuleType("camoufox")
        package.__path__ = []
        self.modules = patch.dict("sys.modules", {
            "camoufox": package, "camoufox.utils": self.utils, "camoufox.addons": self.addons,
        })
        self.modules.start()

    def tearDown(self):
        for browser in self.browsers:
            browser._release_profile_lock()
        self.modules.stop()
        self.temp.cleanup()

    def generate_options(self, *, config, firefox_user_prefs, **_kwargs):
        config.update({
            "navigator.userAgent": "fixture Firefox",
            "navigator.platform": "fixture",
            "navigator.hardwareConcurrency": 8,
            "screen.width": 1920, "screen.height": 1080,
            "fonts": ["font one", "font two"], "voices": ["voice one"],
            "fonts:spacing_seed": 11, "audio:seed": 22, "canvas:seed": 33,
            "webGl:vendor": "fixture vendor", "webGl:renderer": "fixture renderer",
            "addons": ["/old/runtime/addon"],
        })
        return {
            "firefox_user_prefs": {**firefox_user_prefs, "webgl.enable-webgl2": True, "webgl.force-enabled": True},
            "env": {"OPENROUTER_API_KEY": "never-save-this"},
            "executable_path": "/old/runtime/browser",
        }

    def browser(self, headless=True, acquire=True):
        browser = self.Browser(headless=headless, profile_dir=self.root / "profile")
        self.browsers.append(browser)
        if acquire:
            browser._acquire_profile_lock()
        return browser

    def test_restarts_and_display_changes_keep_the_complete_identity(self):
        first = self.browser()
        with patch.dict(os.environ, {"TEST_API_KEY": "process-only-secret"}):
            original_options = first._camoufox_launch_options()
        path = self.root / "profile/camoufox-config.json"
        saved_bytes = path.read_bytes()
        self.assertNotIn(b"process-only-secret", saved_bytes)
        self.assertNotIn(b"never-save-this", saved_bytes)
        self.assertNotIn(b"/old/runtime", saved_bytes)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        original = json.loads(original_options["env"]["CAMOU_CONFIG_1"])
        first._release_profile_lock()

        self.addon_path = "/updated/runtime/addons/UBO"
        self.executable = "/updated/runtime/camoufox"
        second = self.browser(headless=False)
        options = second._camoufox_launch_options()
        restored = json.loads(options["env"]["CAMOU_CONFIG_1"])
        self.assertEqual(restored.pop("addons"), [self.addon_path])
        original.pop("addons")
        self.assertEqual(restored, original)
        self.assertFalse(options["headless"])
        self.assertEqual(options["executable_path"], self.executable)
        self.assertEqual(options["firefox_user_prefs"]["webgl.enable-webgl2"], True)
        self.assertEqual(path.read_bytes(), saved_bytes)
        self.generate.assert_called_once()

    def test_corrupt_or_changed_config_never_generates_a_replacement(self):
        browser = self.browser()
        browser._camoufox_launch_options()
        path = self.root / "profile/camoufox-config.json"
        saved = json.loads(path.read_text())
        changed = copy.deepcopy(saved)
        changed["config"].pop("fonts")
        unknown = copy.deepcopy(saved)
        unknown["version"] = 2
        wrong_type = copy.deepcopy(saved)
        wrong_type["version"] = True
        for value in ("{", json.dumps(changed), json.dumps(unknown), json.dumps(wrong_type)):
            with self.subTest(value=value[:20]):
                path.write_text(value)
                with self.assertRaisesRegex(RuntimeError, "did not generate a replacement"):
                    browser._camoufox_launch_options()
                self.assertEqual(path.read_text(), value)
        self.generate.assert_called_once()

    def test_unsupported_or_incomplete_saved_identity_stops_even_with_a_valid_digest(self):
        browser = self.browser()
        browser._camoufox_launch_options()
        path = self.root / "profile/camoufox-config.json"
        saved = json.loads(path.read_text())
        for missing in (False, True):
            with self.subTest(missing=missing):
                changed = copy.deepcopy(saved)
                if missing:
                    changed["config"].pop("canvas:seed")
                else:
                    changed["config"]["unsupported.property"] = "do-not-print"
                payload = json.dumps({key:changed[key] for key in ("config", "firefox_user_prefs")},
                                     sort_keys=True, separators=(",", ":"), allow_nan=False)
                changed["sha256"] = hashlib.sha256(payload.encode()).hexdigest()
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(RuntimeError, "did not generate a replacement"):
                    browser._camoufox_launch_options()
        self.generate.assert_called_once()

    def test_cancelled_generation_finishes_before_the_profile_lock_is_released(self):
        started = threading.Event()
        release = threading.Event()
        browser = self.browser(acquire=False)
        manager = Mock()
        async_api = types.ModuleType("camoufox.async_api")
        async_api.AsyncCamoufox = manager
        window = types.ModuleType("notte_browser.window")
        window.BrowserResource = Mock()
        window.BrowserWindow = Mock()
        window.BrowserWindowOptions = types.SimpleNamespace(from_request=lambda request: request)
        sdk = types.ModuleType("notte_sdk.types")
        sdk.SessionStartRequest = lambda **kwargs: kwargs

        def build():
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("Test writer was not released")
            self.assertIsNotNone(browser._profile_lock)
            return {}

        async def check():
            task = asyncio.create_task(browser._ensure())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0.01)
                    self.assertFalse(task.done())
                    competitor = self.browser(acquire=False)
                    with self.assertRaisesRegex(RuntimeError, "in use"):
                        competitor._acquire_profile_lock()
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertIsNone(browser._profile_lock)

        with patch.dict("sys.modules", {"notte": types.ModuleType("notte"),
                                       "camoufox.async_api": async_api,
                                       "notte_browser.window": window, "notte_sdk.types": sdk}), \
                patch.object(browser, "_camoufox_launch_options", side_effect=build):
            asyncio.run(check())
        manager.assert_not_called()

    def test_file_requires_the_profile_lock(self):
        browser = self.browser(acquire=False)
        with self.assertRaisesRegex(RuntimeError, "profile lock"):
            browser._camoufox_launch_options()
        self.generate.assert_not_called()
        self.assertFalse((self.root / "profile/camoufox-config.json").exists())

    def test_failed_atomic_replace_leaves_no_partial_configuration(self):
        browser = self.browser()
        with patch.object(self.code["os"], "replace", side_effect=OSError("fixture disk error")):
            with self.assertRaises(OSError):
                browser._camoufox_launch_options()
        self.assertFalse((self.root / "profile/camoufox-config.json").exists())
        self.assertEqual(list((self.root / "profile").glob(".camoufox-config-*")), [])

    def test_process_override_cannot_replace_profile_identity(self):
        browser = self.browser()
        with patch.dict(os.environ, {"CAMOU_CONFIG_1": "process override"}):
            with self.assertRaisesRegex(RuntimeError, "environment overrides conflict"):
                browser._camoufox_launch_options()
        self.generate.assert_not_called()

    def test_existing_fingerprint_permissions_are_private(self):
        browser = self.browser()
        browser._camoufox_launch_options()
        path = self.root / "profile/camoufox-config.json"
        path.chmod(0o644)
        browser._camoufox_launch_options()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


@unittest.skipUnless(os.environ.get("DTT_TEST_BROWSER") == "1", "set DTT_TEST_BROWSER=1 for installed Camoufox checks")
class InstalledFingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_library_reuses_profile_config_across_display_modes(self):
        code = load_browser_code()
        with tempfile.TemporaryDirectory(prefix="dtt-fingerprint-live-", dir="/tmp") as directory:
            profile = Path(directory) / "profile"
            first_bytes = None
            first_identity = None
            for headless in (True, True, False, True):
                browser = code["Browser"](headless=headless, profile_dir=profile)
                try:
                    session = await browser._ensure()
                    page = session.window.page
                    identity = await page.evaluate("""() => ({
                        userAgent:navigator.userAgent,platform:navigator.platform,
                        hardwareConcurrency:navigator.hardwareConcurrency,
                        screenWidth:screen.width,screenHeight:screen.height
                    })""")
                    saved_bytes = (profile / "camoufox-config.json").read_bytes()
                    saved = json.loads(saved_bytes)
                    for key in ("fonts", "fonts:spacing_seed", "audio:seed", "canvas:seed",
                                "navigator.userAgent", "webGl:vendor", "webGl:renderer"):
                        self.assertIn(key, saved["config"])
                    if first_bytes is None:
                        first_bytes, first_identity = saved_bytes, identity
                    else:
                        self.assertEqual(saved_bytes, first_bytes)
                        self.assertEqual(identity, first_identity)
                    self.assertEqual(browser._headless, headless)
                finally:
                    await browser.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
