"""Browser CLI selection without services, credentials, or model calls."""

import argparse
import ast
import asyncio
import contextlib
import io
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
