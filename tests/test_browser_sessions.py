"""Browser session regressions without an LLM, network, or browser process."""

import ast
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_browser_code():
    source = (ROOT / "dtt.sh").read_text().split("<< 'PYTHON_AGENT'\n", 1)[1]
    source = source.split("\nPYTHON_AGENT", 1)[0]
    tree = ast.parse(source)
    definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
    selected = set()

    def select(name):
        node = definitions.get(name)
        if node is None or id(node) in selected:
            return
        selected.add(id(node))
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                select(child.id)

    for name in ("Browser", "_browser_mcp_tools"):
        select(name)
    imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias for alias in node.names
                     if alias.name.split(".")[0] in sys.stdlib_module_names]
            if names:
                imports.append(ast.Import(names=names))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in sys.stdlib_module_names:
                imports.append(node)
    module = ast.Module(
        body=imports + [node for node in tree.body if id(node) in selected],
        type_ignores=[],
    )
    agent = next(node for node in tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "Agent")
    module.body.append(next(node for node in agent.body
                            if isinstance(node, ast.AsyncFunctionDef)
                            and node.name == "_tool_browser_session"))
    namespace = {"__name__": "dtt_browser_test"}
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "dtt.sh"), "exec"), namespace)
    namespace["_configure_redacted_loguru_logging"] = lambda: None
    return namespace


class FakeEvents:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        if callback in self.listeners.get(event, []):
            self.listeners[event].remove(callback)

    def emit(self, event, *args):
        for callback in list(self.listeners.get(event, [])):
            result = callback(*args)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)


class FakeContext(FakeEvents):
    def __init__(self, profile_dir=None):
        super().__init__()
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.state = {"cookies": [], "origins": []}
        if self.profile_dir:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            saved = self.profile_dir / "fake-native-state.json"
            if saved.exists():
                self.state = json.loads(saved.read_text())
        self.restores = []
        self.snapshots = []
        self.pages = []
        self.closed = False
        self.browser = FakeEvents()
        self.browser.is_connected = lambda: not self.closed

    def is_closed(self):
        return self.closed

    async def storage_state(self, *, indexed_db=False):
        if self.closed:
            raise RuntimeError("Browser context is closed")
        self.snapshots.append(indexed_db)
        return copy.deepcopy(self.state)

    async def set_storage_state(self, state):
        if isinstance(state, (str, Path)):
            state = json.loads(Path(state).read_text())
        self.restores.append(copy.deepcopy(state))
        self.state = copy.deepcopy(state)

    async def cookies(self):
        return copy.deepcopy(self.state["cookies"])

    async def add_cookies(self, cookies):
        self.state["cookies"] = copy.deepcopy(cookies)

    async def new_page(self):
        page = FakePage(self)
        self.emit("page", page)
        return page

    async def close(self):
        if self.closed:
            return
        if self.profile_dir:
            (self.profile_dir / "fake-native-state.json").write_text(json.dumps(self.state))
        self.closed = True
        for page in list(self.pages):
            await page.close()
        self.emit("close", self)
        self.browser.emit("disconnected", self.browser)


class FakePage(FakeEvents):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.url = "about:blank"
        self.closed = False
        self.front_count = 0
        self.main_frame = object()
        context.pages.append(self)

    def is_closed(self):
        return self.closed

    async def title(self):
        return "Test page"

    async def bring_to_front(self):
        self.front_count += 1

    async def goto(self, url, **kwargs):
        self.url = url
        self.emit("framenavigated", self.main_frame)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.context.pages.remove(self)
        self.emit("close", self)


class FakeSession:
    instances = []
    fail_next_start = False
    fail_next_close = False

    def __init__(self, *, profile_dir=None, **kwargs):
        self.options = kwargs
        self.context = FakeContext(profile_dir)
        self.window = types.SimpleNamespace(page=FakePage(self.context))
        self.enter_count = 0
        self.exit_count = 0
        self.executions = []
        self.observe_count = 0
        self.__class__.instances.append(self)

    async def __aenter__(self):
        self.enter_count += 1
        if self.__class__.fail_next_start:
            self.__class__.fail_next_start = False
            raise RuntimeError("test browser launch failure")
        return self

    async def __aexit__(self, *args):
        self.exit_count += 1
        if self.__class__.fail_next_close:
            self.__class__.fail_next_close = False
            raise RuntimeError("test native close failure")
        await self.context.close()

    async def aexecute(self, **kwargs):
        self.executions.append(kwargs)
        if kwargs["type"] == "goto":
            await self.window.page.goto(kwargs["url"])
        return types.SimpleNamespace(success=True, message="")

    async def aobserve(self):
        self.observe_count += 1
        return types.SimpleNamespace(space=types.SimpleNamespace(interaction_actions=[]))

    async def ascrape(self, **kwargs):
        return "Authenticated test page"

    async def aget_cookies(self):
        return await self.context.cookies()

    async def aset_cookies(self, *, cookie_file=None, cookies=None):
        await self.context.add_cookies(cookies or json.loads(Path(cookie_file).read_text()))


class FakeAgent:
    sessions = []

    def __init__(self, *, session, **kwargs):
        self.session = session
        self.__class__.sessions.append(session)

    async def arun(self, *, task, **kwargs):
        self.session.context.state["cookies"] = [cookie("agent-login")]
        return types.SimpleNamespace(answer="Agent finished")


def cookie(value):
    return {"name": "login", "value": value, "domain": "example.test", "path": "/",
            "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}


def login_state():
    return {
        "cookies": [cookie("signed-in")],
        "origins": [{
            "origin": "https://example.test",
            "localStorage": [{"name": "account", "value": "saved-account"}],
            "indexedDB": [{"name": "auth", "version": 1, "stores": []}],
        }],
    }


class BrowserSessionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = load_browser_code()
        cls.Browser = cls.code["Browser"]

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dtt-browser-tests-")
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile"
        self.home_patch = patch.object(Path, "home", return_value=self.root)
        self.home_patch.start()
        self.env_patch = patch.dict(os.environ, {"DTT_BROWSER_SESSION": ""})
        self.env_patch.start()
        self.code["BASE"] = self.root
        FakeSession.instances = []
        FakeSession.fail_next_start = False
        FakeSession.fail_next_close = False
        FakeAgent.sessions = []
        self.notte_patch = patch.dict(sys.modules, {"notte": types.SimpleNamespace(Agent=FakeAgent)})
        self.notte_patch.start()

        async def start_session(browser):
            session = FakeSession(profile_dir=browser._profile_dir, headless=browser._headless)
            await session.__aenter__()
            return session

        self.start_patch = patch.object(self.Browser, "_start_session", new=start_session)
        self.start_patch.start()
        self.browsers = []

    async def asyncTearDown(self):
        for browser in self.browsers:
            await browser.close()
        self.start_patch.stop()
        self.notte_patch.stop()
        self.env_patch.stop()
        self.home_patch.stop()
        self.temp.cleanup()

    def browser(self, **kwargs):
        browser = self.Browser(**kwargs)
        self.browsers.append(browser)
        return browser

    async def wait_ready(self, browser):
        status = await asyncio.wait_for(browser.control("wait", timeout_seconds=1), timeout=2)
        self.assertEqual(status["login_state"], "ready", status)
        self.assertFalse(status["headed"])
        self.assertFalse(status["paused"])
        return status

    async def test_native_profile_preserves_login_after_last_tool_call(self):
        browser = self.browser(profile_dir=self.profile)
        session = await browser._ensure()
        await browser.act("observe")
        session.context.state = login_state()
        await browser.close()
        restored = await self.browser(profile_dir=self.profile)._ensure()
        self.assertEqual(restored.context.state, login_state())
        self.assertEqual(session.context.snapshots, [])
        self.assertEqual(restored.context.restores, [])

    async def test_logout_stays_deleted_after_profile_reopen(self):
        browser = self.browser(profile_dir=self.profile)
        session = await browser._ensure()
        session.context.state = login_state()
        await browser.close()
        second = self.browser(profile_dir=self.profile)
        restored = await second._ensure()
        restored.context.state = {"cookies": [], "origins": []}
        await second.close()
        final = await self.browser(profile_dir=self.profile)._ensure()
        self.assertEqual(final.context.state, {"cookies": [], "origins": []})

    async def test_login_opens_headed_and_quit_restarts_headless_with_authentication(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.act("goto", url="https://example.test/account")
        original = FakeSession.instances[-1]
        result = await browser.control("login")
        shown = FakeSession.instances[-1]
        self.assertIsNot(shown, original)
        self.assertFalse(shown.options["headless"])
        self.assertEqual(result["login_state"], "waiting_for_close")
        self.assertEqual(shown.window.page.url, "https://example.test/account")
        shown.context.state = login_state()
        await shown.context.close()
        result = await self.wait_ready(browser)
        self.assertEqual(result["url"], "https://example.test/account")
        resumed = FakeSession.instances[-1]
        self.assertIsNot(resumed, shown)
        self.assertEqual(resumed.context.state, login_state())
        await browser.act("observe")
        self.assertEqual(resumed.observe_count, 1)

    async def test_login_waits_until_every_window_closes(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("login", url="https://example.test/account")
        shown = FakeSession.instances[-1]
        another_page = await shown.context.new_page()
        await another_page.goto("https://example.test/dashboard")
        shown.context.state = login_state()
        await shown.window.page.close()
        await asyncio.sleep(0)
        status = await browser.control("status")
        self.assertEqual(status["login_state"], "waiting_for_close")
        self.assertTrue(status["open"])
        self.assertEqual(status["url"], "https://example.test/dashboard")
        self.assertIs(FakeSession.instances[-1], shown)
        await another_page.close()
        result = await self.wait_ready(browser)
        self.assertEqual(result["url"], "https://example.test/dashboard")
        self.assertEqual(FakeSession.instances[-1].context.state, login_state())

    async def test_profile_lock_remains_held_through_automatic_restart(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("login", url="https://example.test/account")
        shown = FakeSession.instances[-1]
        closing = asyncio.Event()
        release = asyncio.Event()
        original = shown.__aexit__

        async def delayed_close(*args):
            closing.set()
            await release.wait()
            await original(*args)

        shown.__aexit__ = delayed_close
        await shown.context.close()
        await asyncio.wait_for(closing.wait(), timeout=1)
        competitor = self.browser(profile_dir=self.profile)
        try:
            with self.assertRaisesRegex(RuntimeError, "(?i)(use|lock|open)"):
                await competitor._ensure()
        finally:
            release.set()
        await self.wait_ready(browser)

    async def test_wait_timeout_keeps_the_login_monitor_active(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("login", url="https://example.test/account")
        shown = FakeSession.instances[-1]
        status = await browser.control("wait", timeout_seconds=1)
        self.assertEqual(status["login_state"], "waiting_for_close")
        shown.context.state = login_state()
        await shown.context.close()
        await self.wait_ready(browser)

    async def test_close_cancels_login_without_an_unwanted_restart(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("login", url="https://example.test/account")
        count = len(FakeSession.instances)
        shown = FakeSession.instances[-1]
        await browser.control("close")
        await shown.context.close()
        await asyncio.sleep(0)
        self.assertEqual(len(FakeSession.instances), count)
        self.assertFalse((await browser.control("status"))["open"])

    async def test_explicit_close_does_not_cancel_a_concurrent_wait_caller(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("login", url="https://example.test/account")
        waiter = asyncio.create_task(browser.control("wait", timeout_seconds=30))
        await asyncio.sleep(0)
        await browser.control("close")
        status = await asyncio.wait_for(waiter, timeout=1)
        self.assertIsInstance(status, dict)
        self.assertFalse(waiter.cancelled())
        self.assertFalse((await browser.control("status"))["open"])

    async def test_wait_caller_cancellation_keeps_the_login_monitor_active(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("login", url="https://example.test/account")
        waiter = asyncio.create_task(browser.control("wait", timeout_seconds=30))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        shown = FakeSession.instances[-1]
        shown.context.state = login_state()
        await shown.context.close()
        await self.wait_ready(browser)

    async def test_close_during_login_setup_cancels_the_new_monitor(self):
        browser = self.browser(profile_dir=self.profile)
        started = asyncio.Event()
        release = asyncio.Event()
        original_execute = FakeSession.aexecute

        async def delayed_navigation(session, **kwargs):
            started.set()
            await release.wait()
            return await original_execute(session, **kwargs)

        with patch.object(FakeSession, "aexecute", new=delayed_navigation):
            login = asyncio.create_task(browser.control("login", url="https://example.test/account"))
            await asyncio.wait_for(started.wait(), timeout=1)
            close = asyncio.create_task(browser.control("close"))
            try:
                await asyncio.sleep(0)
                self.assertFalse(close.done())
            finally:
                release.set()
            await asyncio.wait_for(asyncio.gather(login, close), timeout=1)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertFalse((await browser.control("status"))["open"])
        self.assertIsNone(browser._login_task)

    async def test_named_selection_survives_close_after_a_temporary_profile(self):
        browser = self.browser()
        temporary = await browser._ensure()
        expected = self.root / ".dtt" / "browser-sessions" / "work" / "profile"
        self.assertNotEqual(temporary.context.profile_dir, expected)
        await browser.control("open", session="work", url="https://example.test/account")
        named = FakeSession.instances[-1]
        named.context.state = login_state()
        await browser.close()
        closed = await browser.control("status")
        self.assertEqual(closed["session"], "work")
        self.assertEqual(closed["profile_dir"], str(expected))
        reopened = await browser._ensure()
        self.assertEqual(reopened.context.profile_dir, expected)
        self.assertEqual(reopened.context.state, login_state())

    async def test_restart_failure_reports_error_and_releases_profile_lock(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("login", url="https://example.test/account")
        shown = FakeSession.instances[-1]
        shown.context.state = login_state()
        FakeSession.fail_next_start = True
        await shown.context.close()
        status = await browser.control("wait", timeout_seconds=1)
        self.assertEqual(status["login_state"], "failed", status)
        self.assertIn("launch failure", status["login_error"])
        restored = await self.browser(profile_dir=self.profile)._ensure()
        self.assertEqual(restored.context.state, login_state())

    async def test_automation_stays_paused_during_user_login(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("login", url="https://example.test/account")
        shown = FakeSession.instances[-1]
        calls = (
            lambda: browser.act("observe"),
            lambda: browser.fetch("https://example.test/other"),
            lambda: browser.agent("Check the account", max_steps=3),
        )
        for call in calls:
            with self.subTest(call=call):
                try:
                    result = await call()
                except RuntimeError as error:
                    result = str(error)
                self.assertIn("waiting_for_close", str(result).lower())
        self.assertEqual(shown.observe_count, 0)
        self.assertEqual(FakeAgent.sessions, [])

    async def test_resume_headless_preserves_authentication_and_current_url(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("open", url="https://example.test/account")
        shown = FakeSession.instances[-1]
        shown.context.state = login_state()
        result = await browser.control("resume", headed=False)
        resumed = FakeSession.instances[-1]
        self.assertIsNot(resumed, shown)
        self.assertTrue(resumed.options["headless"])
        self.assertFalse(result["paused"])
        self.assertEqual(resumed.window.page.url, "https://example.test/account")
        self.assertEqual(resumed.context.state, login_state())

    async def test_invalid_control_arguments_leave_live_session_unchanged(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.control("open", url="https://example.test/account")
        shown = FakeSession.instances[-1]
        shown.context.state = login_state()
        before = await browser.control("status")
        invalid_calls = (
            ("unknown", {}), ("status", {"headed": False}),
            ("close", {"session": "other"}), ("resume", {"session": "other"}),
            ("resume", {"url": "https://example.test/other"}),
            ("open", {"headed": "false"}), ("open", {"session": "../outside"}),
            ("wait", {"timeout_seconds": -1}),
        )
        for action, kwargs in invalid_calls:
            with self.subTest(action=action, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    await browser.control(action, **kwargs)
                self.assertEqual(await browser.control("status"), before)
                self.assertIs(FakeSession.instances[-1], shown)
                self.assertEqual(shown.exit_count, 0)
                self.assertEqual(shown.context.state, login_state())

    async def test_navigation_failure_saves_selected_session_in_thread_metadata(self):
        browser = self.browser(session_name="old")
        await browser._ensure()
        saved = []
        logger = types.SimpleNamespace(
            load_meta=lambda: {"label": "Existing thread"},
            save_meta=lambda metadata: saved.append(metadata),
        )
        agent = types.SimpleNamespace(browser=browser, thread_logger=logger, headed=False)

        async def failed_navigation(self, **kwargs):
            return types.SimpleNamespace(success=False, message="test navigation failed")

        with patch.object(FakeSession, "aexecute", new=failed_navigation):
            with self.assertRaisesRegex(RuntimeError, "test navigation failed"):
                await self.code["_tool_browser_session"](
                    agent, "open", session="new", headed=True, url="https://example.test/account",
                )
        self.assertEqual(browser.session_name, "new")
        self.assertTrue(agent.headed)
        self.assertEqual(saved, [{"label": "Existing thread", "browser_session": "new", "headed": True}])

    async def test_named_session_has_exclusive_lock_until_close(self):
        first = self.browser(session_name="work")
        second = self.browser(session_name="work")
        first_session = await first._ensure()
        first_session.context.state = login_state()
        with self.assertRaisesRegex(RuntimeError, "(?i)(use|lock|open)"):
            await second._ensure()
        await first.close()
        self.assertEqual((await second._ensure()).context.state, login_state())

    async def test_failed_start_can_retry(self):
        browser = self.browser(profile_dir=self.profile)
        FakeSession.fail_next_start = True
        with self.assertRaisesRegex(RuntimeError, "launch failure"):
            await browser._ensure()
        session = await browser._ensure()
        self.assertEqual(len(FakeSession.instances), 2)
        await browser.act("observe")
        self.assertEqual(session.observe_count, 1)

    async def test_invalid_named_sessions_cannot_escape_storage_directory(self):
        for name in ("../outside", "/tmp/outside", "nested/session", "..", ".", "bad\\name"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    await self.browser(session_name=name)._ensure()

    async def test_autonomous_agent_and_browser_actions_share_session(self):
        browser = self.browser(profile_dir=self.profile)
        await browser.act("goto", url="https://example.test")
        session = FakeSession.instances[0]
        await browser.agent("Check the account", max_steps=3)
        await browser.act("observe")
        self.assertEqual(FakeAgent.sessions, [session])
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertEqual(session.context.state["cookies"], [cookie("agent-login")])

    async def test_handoff_waits_for_active_browser_action(self):
        browser = self.browser(profile_dir=self.profile)
        session = await browser._ensure()
        started = asyncio.Event()
        release = asyncio.Event()
        original = session.aexecute

        async def delayed_execute(**kwargs):
            started.set()
            await release.wait()
            return await original(**kwargs)

        session.aexecute = delayed_execute
        active = asyncio.create_task(browser.act("goto", url="https://example.test/form"))
        await started.wait()
        handoff = asyncio.create_task(browser.control("login"))
        try:
            await asyncio.sleep(0)
            self.assertFalse(handoff.done())
            self.assertEqual(session.exit_count, 0)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(active, handoff), timeout=1)
        self.assertEqual(FakeSession.instances[-1].window.page.url, "https://example.test/form")

    async def test_storage_snapshot_migrates_once_after_native_close(self):
        legacy = self.root / "storage.json"
        legacy.write_text(json.dumps(login_state()))
        browser = self.browser()
        browser.set_profile(self.profile, legacy_state_file=legacy)
        session = await browser._ensure()
        self.assertEqual(session.context.state, login_state())
        self.assertTrue(legacy.exists())
        await browser.act("observe")
        self.assertEqual(len(session.context.restores), 1)
        self.assertTrue(legacy.exists())
        await browser.close()
        self.assertFalse(legacy.exists())
        self.assertEqual((await self.browser(profile_dir=self.profile)._ensure()).context.state, login_state())

    async def test_old_thread_cookies_migrate_after_native_close(self):
        legacy = self.root / "browser_cookies.json"
        legacy.write_text(json.dumps([cookie("old-thread")]))
        browser = self.browser()
        browser.set_profile(self.profile, legacy_cookie_file=legacy)
        session = await browser._ensure()
        self.assertEqual(session.context.state["cookies"], [cookie("old-thread")])
        self.assertTrue(legacy.exists())
        await browser.close()
        self.assertFalse(legacy.exists())
        restored = await self.browser(profile_dir=self.profile)._ensure()
        self.assertEqual(restored.context.state["cookies"], [cookie("old-thread")])

    async def test_failed_native_close_preserves_migration_source_and_releases_lock(self):
        legacy = self.root / "storage.json"
        legacy.write_text(json.dumps(login_state()))
        browser = self.browser()
        browser.set_profile(self.profile, legacy_state_file=legacy)
        session = await browser._ensure()
        FakeSession.fail_next_close = True
        with self.assertRaisesRegex(RuntimeError, "native close failure"):
            await browser.close()
        self.assertTrue(legacy.exists())
        await session.context.close()
        restored = await self.browser(profile_dir=self.profile)._ensure()
        self.assertEqual(restored.context.state, login_state())

    def test_mcp_exposes_login_monitor_controls(self):
        tools = self.code["_browser_mcp_tools"](types.SimpleNamespace(Tool=types.SimpleNamespace))
        controls = next(tool for tool in tools if tool.name == "dtt_browser_session")
        properties = controls.inputSchema["properties"]
        self.assertEqual(set(properties["action"]["enum"]), {"status", "open", "resume", "close", "login", "wait"})
        self.assertEqual(properties["session"]["type"], "string")
        self.assertEqual(properties["headed"]["type"], "boolean")
        self.assertIn("timeout_seconds", properties)
        self.assertIn("action", controls.inputSchema["required"])


if __name__ == "__main__":
    unittest.main()
