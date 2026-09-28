import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import service

class FakeInput:
    def write(self, data): pass
    async def drain(self): pass

class FakeOutput:
    def __init__(self, rows):
        self.rows = iter(rows)
    async def readline(self):
        return next(self.rows, b"")

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workspace = self.root / "key"
        self.workspace.mkdir()
        service.ROOT = self.root
        service.lock = asyncio.Lock()
        service.login = None

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def execute(self, events):
        proc = type("Process", (), {})()
        proc.wait = AsyncMock(return_value=1)
        proc.stdin = FakeInput()
        proc.stdout = FakeOutput([(json.dumps(e) + "\n").encode() for e in events])
        proc.stderr = type("Stderr", (), {"read": AsyncMock(return_value=b"")})()
        stopped = AsyncMock()
        with patch.object(service.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), patch.object(service, "stop", stopped):
            response = await service.turn(service.Turn(prompt="hello", model="gemini-test", workspace=str(self.workspace)))
            rows = [json.loads(chunk) async for chunk in response.body_iterator]
        self.assertTrue(stopped.await_count)
        self.assertFalse(service.lock.locked())
        return rows

    async def test_usage_excludes_prior_turns(self):
        rows = await self.execute([
            {"event": "init", "conversation_id": "thread"},
            {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "OK", "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 10}}},
            {"event": "result", "result": {"status": "SUCCESS", "usage": {"input_tokens": 1007, "output_tokens": 303}}},
        ])
        self.assertEqual(rows[-1]["input_tokens"], 17)
        self.assertEqual(rows[-1]["cache_read_tokens"], 10)
        self.assertEqual(rows[-1]["output_tokens"], 3)

    async def test_quota_failure_never_success(self):
        rows = await self.execute([{"event": "result", "result": {"status": "FAILED", "error": "RESOURCE_EXHAUSTED: quota"}}])
        self.assertEqual(rows[-1]["kind"], "limit")
        self.assertNotIn("done", rows[-1])

    async def test_eof_never_success(self):
        rows = await self.execute([])
        self.assertEqual(rows[-1]["kind"], "connection")
        self.assertNotIn("done", rows[-1])

    def test_login_screen_redacts_submitted_code(self):
        login = service.Login()
        login.redacted.append("private-code")
        login.stream.feed("private-code")
        self.assertNotIn("private-code", json.dumps(login.state()))
        self.assertNotIn("screen", login.state())

    def test_structured_login_steps(self):
        view = service.login_view("Select login method:\n > 1. Continue with Google Cloud\n   2. Other sign-in options")
        self.assertEqual(view["stage"], "choose")
        self.assertEqual(view["selected"], 0)
        self.assertEqual(view["options"][0]["label"], "使用 Google Cloud 企业账号登录")
        view = service.login_view("After authenticating, copy the code displayed in the browser and paste it below:", "https://accounts.google.com/o/oauth2/auth?test=1")
        self.assertEqual(view["stage"], "authorize")
        view = service.login_view("Select project:\n  1. project-a\n> 2. project-b", "https://accounts.google.com/old")
        self.assertEqual(view["title"], "选择 Google Cloud 项目")
        self.assertNotIn("login_url", view)

    def test_startup_signing_in_is_not_logout_success(self):
        self.assertFalse(service.signed_out_screen("Welcome back! You are currently not signed in.\nSigning in..."))
        self.assertFalse(service.signed_out_screen("You are currently not signed in."))
        self.assertTrue(service.signed_out_screen("Select login method:"))

    async def test_logout_already_signed_out_is_success(self):
        proc = type("Process", (), {"returncode": None})()
        with patch.object(service.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
             patch.object(service.os, "read", return_value=b"Welcome back! You are currently not signed in.\r\nSelect login method:"), \
             patch.object(service.os, "write") as write, patch.object(service, "stop", AsyncMock()):
            await service.cli_panel("/logout")
            write.assert_not_called()
        self.assertFalse(service.lock.locked())

    async def test_stale_login_choice_is_rejected(self):
        login = service.Login()
        login.fd = 123
        login.stream.feed("Select login method:\r\n> 1. Continue with Google Cloud\r\n  2. Other sign-in options")
        service.login = login
        with patch.object(service.os, "write") as write:
            with self.assertRaises(service.HTTPException):
                await service.login_input(service.LoginInput(session_id=login.id, action="select:0", menu_id="stale"))
            write.assert_not_called()
            await service.login_input(service.LoginInput(session_id=login.id, action="select:0", menu_id=login.state()["menu_id"]))
            write.assert_called_once_with(123, b"\r")

    def test_quota_and_auth_classification(self):
        self.assertEqual(service.error_kind("429 too many requests"), "limit")
        self.assertEqual(service.error_kind("UNAUTHENTICATED"), "logged_out")

if __name__ == "__main__":
    unittest.main()

class AccountManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_enterprise_usage_is_unknown_not_zero(self):
        value = service.usage_panel("Models & Quota\nFor Antigravity Business consumption options, see")
        self.assertFalse(value["available"])
        self.assertEqual(value["buckets"], [])

    async def test_logout_only_clears_metadata_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            account = Path(directory) / "account.json"
            account.write_text("{}")
            with patch.object(service, "ACCOUNT", account), patch.object(service, "cli_panel", AsyncMock(side_effect=RuntimeError("busy"))):
                with self.assertRaises(RuntimeError):
                    await service.logout()
                self.assertTrue(account.exists())
            with patch.object(service, "ACCOUNT", account), patch.object(service, "cli_panel", AsyncMock(return_value="Signed out from server: test")):
                value = await service.logout()
                self.assertFalse(value["logged_in"])
                self.assertFalse(account.exists())

class LoginCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_reader_cleanup(self):
        session = service.Login()
        session.fd = 123
        session.process = object()
        service.login = session
        released = asyncio.Event()
        async def reader():
            await released.wait()
            await asyncio.sleep(.01)
            session.fd = None
        session.task = asyncio.create_task(reader())
        async def stop_process(process):
            released.set()
        with patch.object(service, "stop", stop_process):
            await service.login_input(service.LoginInput(session_id=session.id, action="cancel"))
        self.assertTrue(session.task.done())
        self.assertIsNone(session.fd)
        service.login = None

    async def test_logout_closes_previous_login_before_cli(self):
        session = service.Login()
        service.login = session
        order = []
        async def close(value):
            self.assertIs(value, session)
            order.append("close")
        async def panel(command):
            order.append("logout")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(service, "close_login", close), patch.object(service, "cli_panel", panel), patch.object(service, "ACCOUNT", Path(directory)/"account"):
                await service.logout()
        self.assertEqual(order, ["close", "logout"])
        self.assertIsNone(service.login)

class LoginVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_verification_runs_once_for_repeated_polls(self):
        session = service.Login()
        session.account = {"email": "test@example.test"}
        session.task = asyncio.create_task(asyncio.sleep(0))
        await session.task
        service.login = session
        result = {"account": session.account}
        with patch.object(service, "probe", AsyncMock(return_value=result)) as probe:
            body = service.LoginInput(session_id=session.id)
            results = await asyncio.gather(service.login_verify(body), service.login_verify(body))
            self.assertEqual(results, [result, result])
            probe.assert_awaited_once()
        service.login = None

    async def test_login_not_success_until_cleanup_finishes(self):
        session = service.Login()
        session.account = {"email": "test@example.test"}
        session.task = asyncio.create_task(asyncio.sleep(0))
        self.assertFalse(session.state()["logged_in"])
        await session.task
        self.assertTrue(session.state()["logged_in"])
