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


class FakeContext:
    def __init__(self):
        self.state = {"cookies": [], "origins": []}
        self.restores = []
        self.snapshots = []
        self.pages = []
        self.browser = types.SimpleNamespace(is_connected=lambda: True)

    async def storage_state(self, *, indexed_db=False):
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


class FakePage:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.closed = False
        self.front_count = 0
        context.pages.append(self)

    def is_closed(self):
        return self.closed

    async def title(self):
        return "Test page"

    async def bring_to_front(self):
        self.front_count += 1

    async def goto(self, url, **kwargs):
        self.url = url


class FakeSession:
    instances = []
    fail_next_start = False

    def __init__(self, **kwargs):
        self.options = kwargs
        self.context = FakeContext()
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
        self.window.page.closed = True

    async def aexecute(self, **kwargs):
        self.executions.append(kwargs)
        if kwargs["type"] == "goto":
            self.window.page.url = kwargs["url"]
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
        self.path = self.root / "storage.json"
        self.home_patch = patch.object(Path, "home", return_value=self.root)
        self.home_patch.start()
        self.env_patch = patch.dict(os.environ, {"DTT_BROWSER_SESSION": ""})
        self.env_patch.start()
        self.code["BASE"] = self.root
        FakeSession.instances = []
        FakeSession.fail_next_start = False
        FakeAgent.sessions = []
        self.notte_patch = patch.dict(sys.modules, {"notte": types.SimpleNamespace(
            Session=FakeSession, Agent=FakeAgent,
        )})
        self.notte_patch.start()
        self.browsers = []

    async def asyncTearDown(self):
        for browser in self.browsers:
            await browser.close()
        self.notte_patch.stop()
        self.env_patch.stop()
        self.home_patch.stop()
        self.temp.cleanup()

    def browser(self, **kwargs):
        browser = self.Browser(**kwargs)
        self.browsers.append(browser)
        return browser

    async def test_restores_full_state_once_and_retains_live_changes(self):
        self.path.write_text(json.dumps(login_state()))
        browser = self.browser(state_file=self.path)
        session = await browser._ensure()
        self.assertEqual(session.context.state, login_state())
        session.context.state["origins"][0]["localStorage"][0]["value"] = "new-account"
        await browser.act("observe")
        await browser.act("observe")
        self.assertEqual(len(session.context.restores), 1)
        self.assertEqual(session.context.state["origins"][0]["localStorage"][0]["value"], "new-account")
        self.assertTrue(session.context.snapshots)
        self.assertTrue(all(session.context.snapshots))

    async def test_logout_replaces_saved_state_without_old_cookies(self):
        self.path.write_text(json.dumps(login_state()))
        browser = self.browser(state_file=self.path)
        session = await browser._ensure()
        session.context.state = {"cookies": [], "origins": []}
        await browser.act("observe")
        await browser.close()
        restored = await self.browser(state_file=self.path)._ensure()
        self.assertEqual(restored.context.state, {"cookies": [], "origins": []})

    async def test_close_captures_login_after_last_tool_call(self):
        browser = self.browser(state_file=self.path)
        session = await browser._ensure()
        session.context.state = login_state()
        await browser.close()
        restored = await self.browser(state_file=self.path)._ensure()
        self.assertEqual(restored.context.state, login_state())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_manual_open_restarts_headed_and_pauses_until_resume(self):
        browser = self.browser(state_file=self.path)
        original = await browser._ensure()
        original.window.page.url = "https://example.test/account"
        original.context.state = login_state()
        await browser.control("open")
        shown = FakeSession.instances[-1]
        self.assertIsNot(shown, original)
        self.assertFalse(shown.options["headless"])
        self.assertEqual(shown.window.page.url, "https://example.test/account")
        self.assertEqual(shown.context.state, login_state())
        paused_calls = (
            lambda: browser.act("observe"),
            lambda: browser.fetch("https://example.test/other"),
            lambda: browser.agent("Check the account", url=None, max_steps=3),
        )
        for call in paused_calls:
            try:
                result = await call()
            except RuntimeError as error:
                result = str(error)
            self.assertIn("resume", str(result).lower())
        self.assertEqual(shown.observe_count, 0)
        self.assertEqual(FakeAgent.sessions, [])
        shown.context.state["cookies"] = [cookie("manual-login")]
        await browser.control("resume")
        await browser.act("observe")
        self.assertEqual(shown.observe_count, 1)
        await browser.close()
        restored = await self.browser(state_file=self.path)._ensure()
        self.assertEqual(restored.context.state["cookies"], [cookie("manual-login")])

    async def test_failed_start_can_retry(self):
        browser = self.browser(state_file=self.path)
        FakeSession.fail_next_start = True
        with self.assertRaisesRegex(RuntimeError, "launch failure"):
            await browser._ensure()
        session = await browser._ensure()
        self.assertEqual(session.enter_count, 1)
        self.assertEqual(len(FakeSession.instances), 2)
        await browser.act("observe")
        self.assertEqual(session.observe_count, 1)

    async def test_resume_headless_retains_authentication_and_current_url(self):
        for persisted in (True, False):
            with self.subTest(persisted=persisted):
                browser = self.browser(state_file=self.path if persisted else None)
                await browser.control("open", url="https://example.test/account")
                shown = FakeSession.instances[-1]
                shown.context.state = login_state()
                result = await browser.control("resume", headed=False)
                resumed = FakeSession.instances[-1]
                self.assertIsNot(resumed, shown)
                self.assertTrue(resumed.options["headless"])
                self.assertFalse(result["paused"])
                self.assertFalse(result["headed"])
                self.assertEqual(resumed.window.page.url, "https://example.test/account")
                self.assertEqual(resumed.context.state, login_state())
                await browser.act("observe")
                self.assertEqual(resumed.observe_count, 1)
                await browser.close()

    async def test_invalid_control_arguments_leave_live_session_unchanged(self):
        browser = self.browser(state_file=self.path)
        await browser.control("open", url="https://example.test/account")
        shown = FakeSession.instances[-1]
        shown.context.state = login_state()
        before = await browser.control("status")
        invalid_calls = (
            ("unknown", {}),
            ("status", {"headed": False}),
            ("close", {"headed": False}),
            ("close", {"session": "other"}),
            ("resume", {"session": "other"}),
            ("resume", {"url": "https://example.test/other"}),
            ("open", {"headed": "false"}),
            ("open", {"session": "../outside"}),
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
        second_session = await second._ensure()
        self.assertEqual(second_session.context.state, login_state())

    async def test_invalid_named_sessions_cannot_escape_storage_directory(self):
        for name in ("../outside", "/tmp/outside", "nested/session", "..", ".", "bad\\name"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    browser = self.browser(session_name=name)
                    await browser._ensure()

    async def test_autonomous_agent_and_browser_actions_share_session(self):
        browser = self.browser(state_file=self.path)
        await browser.act("goto", url="https://example.test")
        session = FakeSession.instances[0]
        await browser.agent("Check the account", url=None, max_steps=3)
        await browser.act("observe")
        self.assertEqual(FakeAgent.sessions, [session])
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertEqual(session.context.state["cookies"], [cookie("agent-login")])

    async def test_control_waits_for_browser_action_before_manual_handoff(self):
        browser = self.browser(state_file=self.path)
        session = await browser._ensure()
        started = asyncio.Event()
        release = asyncio.Event()
        original_execute = session.aexecute

        async def delayed_execute(**kwargs):
            started.set()
            await release.wait()
            return await original_execute(**kwargs)

        session.aexecute = delayed_execute
        active = asyncio.create_task(browser.act("goto", url="https://example.test/form"))
        await started.wait()
        handoff = asyncio.create_task(browser.control("open"))
        try:
            await asyncio.sleep(0)
            self.assertFalse(handoff.done())
            self.assertEqual(session.exit_count, 0)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(active, handoff), timeout=1)
        shown = FakeSession.instances[-1]
        self.assertEqual(shown.window.page.url, "https://example.test/form")

    async def test_old_thread_cookies_migrate_after_successful_state_save(self):
        legacy = self.root / "browser_cookies.json"
        legacy.write_text(json.dumps([cookie("old-thread")]))
        browser = self.browser()
        browser.set_state_file(self.path, legacy_cookie_file=legacy)
        session = await browser._ensure()
        self.assertEqual(session.context.state["cookies"], [cookie("old-thread")])
        self.assertTrue(legacy.exists())
        await browser.act("observe")
        self.assertFalse(legacy.exists())
        self.assertEqual(json.loads(self.path.read_text())["cookies"], [cookie("old-thread")])
        session.context.state = {"cookies": [], "origins": []}
        await browser.close()
        reopened = self.browser()
        reopened.set_state_file(self.path, legacy_cookie_file=legacy)
        restored = await reopened._ensure()
        self.assertEqual(restored.context.state["cookies"], [])

    async def test_snapshot_failure_closes_browser_and_releases_session_lock(self):
        browser = self.browser(state_file=self.path)
        session = await browser._ensure()
        session.context.state = login_state()
        await browser.act("observe")

        async def failed_snapshot(**kwargs):
            raise RuntimeError("test snapshot failure")

        session.context.storage_state = failed_snapshot
        with self.assertRaisesRegex(RuntimeError, "snapshot failure"):
            await browser.close()
        self.assertEqual(session.exit_count, 1)
        reopened = await self.browser(state_file=self.path)._ensure()
        self.assertEqual(reopened.context.state, login_state())

    async def test_failed_snapshot_preserves_legacy_cookie_file(self):
        legacy = self.root / "browser_cookies.json"
        legacy.write_text(json.dumps([cookie("old-thread")]))
        browser = self.browser()
        browser.set_state_file(self.path, legacy_cookie_file=legacy)
        session = await browser._ensure()

        async def failed_snapshot(**kwargs):
            raise RuntimeError("test snapshot failure")

        session.context.storage_state = failed_snapshot
        with self.assertRaisesRegex(RuntimeError, "snapshot failure"):
            await browser.close()
        self.assertEqual(json.loads(legacy.read_text()), [cookie("old-thread")])
        self.assertFalse(self.path.exists())

    def test_mcp_exposes_session_controls(self):
        tools = self.code["_browser_mcp_tools"](types.SimpleNamespace(Tool=types.SimpleNamespace))
        controls = next(tool for tool in tools if tool.name == "dtt_browser_session")
        properties = controls.inputSchema["properties"]
        self.assertEqual(set(properties["action"]["enum"]), {"status", "open", "resume", "close"})
        self.assertEqual(properties["session"]["type"], "string")
        self.assertEqual(properties["headed"]["type"], "boolean")
        self.assertIn("action", controls.inputSchema["required"])


if __name__ == "__main__":
    unittest.main()
