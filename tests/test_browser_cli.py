"""Browser CLI selection without services, credentials, or model calls."""

import argparse
import ast
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class BrowserCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "dtt.sh").read_text().split("<< 'PYTHON_AGENT'\n", 1)[1]
        source = source.split("\nPYTHON_AGENT", 1)[0]
        tree = ast.parse(source)
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        cls.main_code = compile(ast.Module(body=[main], type_ignores=[]), "dtt.sh", "exec")

    def run_cli(self, arguments, *, environment=None, metadata=None):
        calls = []

        async def run_agent(**kwargs):
            calls.append(("agent", kwargs))

        async def run_browser_mcp(**kwargs):
            calls.append(("mcp", kwargs))

        class OrchestratorApp:
            def __init__(self, **kwargs):
                calls.append(("orchestrator", kwargs))

            def run(self):
                pass

        namespace = {
            "__file__": str(ROOT / "dtt.sh"),
            "argparse": argparse, "asyncio": asyncio, "os": os,
            "sys": sys, "Path": Path,
            "MAX_LOOPS": 200, "QUICK_MAX_LOOPS": 15,
            "NORMAL_MAIN": "main", "NORMAL_ORACLE": "oracle",
            "ADVANCED_MAIN": "advanced", "ADVANCED_ORACLE": "advanced-oracle",
            "QUICK_MODEL": "quick", "WORKER_DEFAULT": "worker",
            "BROWSER_AGENT_MODEL_DEFAULT": "browser", "BROWSER_AGENT_MODEL": "browser",
            "PERCEPTION_MODEL": "perception", "_PERCEPTION_AT_IMPORT": "perception",
            "MODEL_ROLES": ("main", "worker", "oracle", "browser"),
            "_parse_model_overrides": lambda arguments: {},
            "ThreadLogger": lambda **kwargs: types.SimpleNamespace(
                load_meta=lambda: dict(metadata or {})),
            "run_agent": run_agent, "run_browser_mcp": run_browser_mcp,
            "OrchestratorApp": OrchestratorApp,
        }
        exec(self.main_code, namespace)
        env = {"OPENROUTER_API_KEY": "test-unused-key", **(environment or {})}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(sys, "argv", ["dtt", *arguments]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            namespace["main"]()
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_mcp_defaults_to_saved_default_session_and_headless(self):
        target, options = self.run_cli(["--browsermcp"])
        self.assertEqual(target, "mcp")
        self.assertEqual(options, {"browser_session": "default", "headed": False})

    def test_mcp_receives_explicit_session_and_headed_mode(self):
        target, options = self.run_cli(
            ["--browsermcp", "--browser-session", "work", "--headed"],
            environment={"DTT_BROWSER_SESSION": "other"})
        self.assertEqual(target, "mcp")
        self.assertEqual(options, {"browser_session": "work", "headed": True})

    def test_mcp_receives_environment_session_and_explicit_headless_mode(self):
        _, options = self.run_cli(["--browsermcp", "--headless"],
                                  environment={"DTT_BROWSER_SESSION": "work"})
        self.assertEqual(options, {"browser_session": "work", "headed": False})

    def test_display_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as error:
            self.run_cli(["--browsermcp", "--headed", "--headless"])
        self.assertEqual(error.exception.code, 2)

    def test_new_agent_defaults_to_its_own_thread_and_headless(self):
        target, options = self.run_cli(["--prompt", "Check the account"])
        self.assertEqual(target, "agent")
        self.assertIsNone(options["browser_session"])
        self.assertFalse(options["headed"])

    def test_new_agent_uses_environment_session(self):
        _, options = self.run_cli(["--prompt", "Check the account"],
                                  environment={"DTT_BROWSER_SESSION": "work"})
        self.assertEqual(options["browser_session"], "work")

    def test_resumed_agent_inherits_session_and_display_ahead_of_environment(self):
        _, options = self.run_cli(
            ["--resume", "saved-thread", "--prompt", "Continue"],
            metadata={"browser_session": "saved", "headed": True},
            environment={"DTT_BROWSER_SESSION": "other"})
        self.assertEqual(options["browser_session"], "saved")
        self.assertTrue(options["headed"])

    def test_resumed_private_thread_does_not_switch_to_environment_session(self):
        _, options = self.run_cli(
            ["--resume", "saved-thread", "--prompt", "Continue"],
            metadata={"browser_session": None, "headed": False},
            environment={"DTT_BROWSER_SESSION": "other"})
        self.assertIsNone(options["browser_session"])

    def test_explicit_flags_override_resumed_session_and_display(self):
        _, options = self.run_cli(
            ["--resume", "saved-thread", "--prompt", "Continue",
             "--browser-session", "new", "--headless"],
            metadata={"browser_session": "saved", "headed": True},
            environment={"DTT_BROWSER_SESSION": "other"})
        self.assertEqual(options["browser_session"], "new")
        self.assertFalse(options["headed"])

    def test_orchestrator_receives_session_and_display_settings(self):
        target, options = self.run_cli(
            ["--orchestrator", "--browser-session", "work", "--headed"])
        self.assertEqual(target, "orchestrator")
        self.assertEqual(options["browser_session"], "work")
        self.assertTrue(options["headed"])


class BrowserLoginGateTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "dtt.sh").read_text().split("<< 'PYTHON_AGENT'\n", 1)[1]
        source = source.split("\nPYTHON_AGENT", 1)[0]
        tree = ast.parse(source)
        agent = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "Agent")
        helper = next(node for node in agent.body
                      if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "_await_browser_login")
        namespace = {"asyncio": asyncio, "json": json}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "dtt.sh", "exec"), namespace)
        cls.await_login = staticmethod(namespace["_await_browser_login"])

    @staticmethod
    def tool_call(action="login", *, name="browser_session", call_id="login-call"):
        return {"id": call_id, "function": {
            "name": name, "arguments": json.dumps({"action": action}),
        }}

    def agent(self, monitor, state, *, quick=False):
        return types.SimpleNamespace(
            quick=quick,
            browser=types.SimpleNamespace(_login_task=monitor),
            _tool_browser_session=AsyncMock(side_effect=lambda action: json.dumps(state)),
            _call_model=AsyncMock(side_effect=AssertionError("The login gate must not call a model")),
            events=types.SimpleNamespace(emit=Mock()),
        )

    @staticmethod
    async def stop_tasks(*tasks):
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_normal_and_quick_wait_without_model_calls_then_update_login_result(self):
        for quick in (False, True):
            with self.subTest(quick=quick):
                closed = asyncio.Event()
                state = {"login_state": "waiting_for_close", "headed": True, "paused": True}

                async def monitor_login():
                    await closed.wait()
                    state.update(login_state="ready", headed=False, paused=False)

                monitor = asyncio.create_task(monitor_login())
                agent = self.agent(monitor, state, quick=quick)
                calls = [self.tool_call(), self.tool_call(name="fetch_page", call_id="fetch-call")]
                results = [
                    {"tool_call_id": "login-call", "content": "Login window opened"},
                    {"tool_call_id": "fetch-call", "content": "Unrelated result"},
                ]
                gate = asyncio.create_task(self.await_login(agent, calls, results))
                self.addAsyncCleanup(self.stop_tasks, monitor, gate)
                await asyncio.sleep(0)
                self.assertFalse(gate.done())
                agent._call_model.assert_not_awaited()
                agent._tool_browser_session.assert_not_awaited()
                self.assertEqual(results[0]["content"], "Login window opened")

                closed.set()
                await asyncio.wait_for(gate, timeout=1)
                agent._call_model.assert_not_awaited()
                agent._tool_browser_session.assert_awaited_once_with("status")
                self.assertIn('"login_state": "ready"', results[0]["content"])
                self.assertIn('"headed": false', results[0]["content"])
                self.assertEqual(results[1]["content"], "Unrelated result")
                agent.events.emit.assert_called_once_with(
                    "status", phase="running", detail="Browser login handoff finished.")

    async def test_already_completed_login_still_reports_final_state(self):
        monitor = asyncio.create_task(asyncio.sleep(0))
        await monitor
        agent = self.agent(monitor, {"login_state": "ready", "paused": False})
        results = [{"content": "Login started"}]
        await self.await_login(agent, [self.tool_call()], results)
        self.assertIn('"login_state": "ready"', results[0]["content"])
        agent._call_model.assert_not_awaited()

    async def test_unrelated_tools_do_not_wait_for_active_login(self):
        monitor = asyncio.create_task(asyncio.Event().wait())
        self.addAsyncCleanup(self.stop_tasks, monitor)
        agent = self.agent(monitor, {"login_state": "waiting_for_close"})
        results = [{"content": "Current status"}, {"content": "Page content"}]
        calls = [self.tool_call(action="status"), self.tool_call(name="fetch_page")]
        await asyncio.wait_for(self.await_login(agent, calls, results), timeout=1)
        self.assertFalse(monitor.done())
        self.assertEqual(results, [{"content": "Current status"}, {"content": "Page content"}])
        agent._tool_browser_session.assert_not_awaited()
        agent.events.emit.assert_not_called()

    async def test_failed_login_start_without_monitor_does_not_wait(self):
        agent = self.agent(None, {"login_state": None})
        results = [{"content": "Error opening browser"}]
        await self.await_login(agent, [self.tool_call()], results)
        self.assertEqual(results[0]["content"], "Error opening browser")
        agent._tool_browser_session.assert_not_awaited()

    async def test_failed_handoff_reports_error_before_next_model_turn(self):
        monitor = asyncio.create_task(asyncio.sleep(0))
        await monitor
        agent = self.agent(monitor, {"login_state": "failed", "login_error": "Browser restart failed"})
        results = [{"content": "Login started"}]
        await self.await_login(agent, [self.tool_call()], results)
        self.assertIn('"login_state": "failed"', results[0]["content"])
        self.assertIn("Browser restart failed", results[0]["content"])
        agent._call_model.assert_not_awaited()

    async def test_caller_cancellation_keeps_background_monitor_alive(self):
        closed = asyncio.Event()
        monitor = asyncio.create_task(closed.wait())
        agent = self.agent(monitor, {"login_state": "waiting_for_close"})
        results = [{"content": "Login started"}]
        gate = asyncio.create_task(self.await_login(agent, [self.tool_call()], results))
        self.addAsyncCleanup(self.stop_tasks, monitor, gate)
        await asyncio.sleep(0)
        gate.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await gate
        self.assertFalse(monitor.done())
        self.assertEqual(results[0]["content"], "Login started")
        agent._tool_browser_session.assert_not_awaited()
        closed.set()
        await asyncio.wait_for(monitor, timeout=1)
        self.assertFalse(monitor.cancelled())


if __name__ == "__main__":
    unittest.main()
