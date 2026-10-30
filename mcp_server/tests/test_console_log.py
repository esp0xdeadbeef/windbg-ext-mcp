#!/usr/bin/env python
"""
Tests for the full console transcript feature.

`run_command` returns the output of ONE command, capped. `get_console_log`
returns the accumulated session transcript. These tests pin the contract that
matters and that is easy to undo by accident:

  1. the tool exists and takes no way to CLEAR the log (a transcript is
     evidence; nothing may wipe it through MCP),
  2. `max_bytes` / `last_command` / `instance` are forwarded to the extension
     handler verbatim,
  3. an ambiguous multi-session bridge REFUSES rather than returning some other
     target's transcript (the log names a machine, so a wrong pick looks right).
"""
import ast
import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.execution_tools as et  # noqa: E402


TOOL_SRC = inspect.getsource(et.register_execution_tools)


class TestConsoleLogToolSurface(unittest.TestCase):
    def test_tool_is_registered(self):
        self.assertIn("async def get_console_log(", TOOL_SRC)

    def test_no_clear_parameter(self):
        """No `.cls`/`.clear` equivalent may be reachable from MCP."""
        tree = ast.parse(TOOL_SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_console_log":
                names = {a.arg for a in node.args.args + node.args.kwonlyargs}
                self.assertIn("max_bytes", names)
                self.assertIn("last_command", names)
                self.assertNotIn("clear", names,
                                 "get_console_log must not expose a clear/wipe option")
                return
        self.fail("get_console_log not found")

    def test_parameters_forwarded(self):
        """max_bytes / last_command / instance reach the extension handler."""
        captured = {}

        def fake_handler(handler_name, timeout_ms=None, instance=None, **kwargs):
            captured.update(handler=handler_name, instance=instance, **kwargs)
            return {"status": "success", "output": "0: kd> lm\nstart end",
                    "truncated": True}

        calls = []

        # Capture the coroutine functions register_execution_tools defines, so
        # they can be awaited without standing up a real FastMCP server.
        class FakeMCP:
            def tool(self, *a, **k):
                def deco(fn):
                    calls.append(fn)
                    return fn
                return deco

        et.register_execution_tools(FakeMCP())
        fn = next(f for f in calls if f.__name__ == "get_console_log")

        import asyncio
        with mock.patch("core.communication.send_handler_command",
                        side_effect=fake_handler), \
             mock.patch.object(et, "list_windbg_instances",
                               return_value=["\\\\.\\pipe\\windbgmcp.42"]):
            result = asyncio.get_event_loop().run_until_complete(
                fn(ctx=None, max_bytes=123, last_command=True))

        self.assertTrue(result["success"])
        self.assertEqual(captured["handler"], "get_console_log")
        self.assertEqual(captured["max_bytes"], 123)
        self.assertTrue(captured["last_command"])
        self.assertNotIn("clear", captured)
        self.assertEqual(captured["instance"], "\\\\.\\pipe\\windbgmcp.42")
        self.assertTrue(result["truncated"])

    def test_ambiguous_sessions_refuse(self):
        """Two live sessions and no pin => refuse, never guess."""
        calls = []

        class FakeMCP:
            def tool(self, *a, **k):
                def deco(fn):
                    calls.append(fn)
                    return fn
                return deco

        et.register_execution_tools(FakeMCP())
        fn = next(f for f in calls if f.__name__ == "get_console_log")

        import asyncio
        with mock.patch.object(et, "list_windbg_instances",
                               return_value=["a", "b"]):
            result = asyncio.get_event_loop().run_until_complete(fn(ctx=None))

        self.assertFalse(result["success"])
        self.assertIn("ambiguous", result["error"])
        self.assertEqual(result["instances"], ["a", "b"])

    def test_run_command_full_output_flag(self):
        """run_command grew full_output / max_output_bytes, defaults unchanged."""
        tree = ast.parse(TOOL_SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_command":
                args = [a.arg for a in node.args.args]
                defaults = dict(zip(args[-len(node.args.defaults):],
                                    node.args.defaults))
                self.assertIn("full_output", args)
                self.assertIn("max_output_bytes", args)
                self.assertEqual(ast.literal_eval(defaults.get("full_output")), False)
                self.assertEqual(ast.literal_eval(defaults.get("max_output_bytes")), 0)
                return
        self.fail("run_command not found")


if __name__ == "__main__":
    unittest.main()
