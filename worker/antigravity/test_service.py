import asyncio
import json
import tempfile
from contextlib import nullcontext
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
        self.configuration = patch.object(service.ToolBridge, "configuration", return_value=nullcontext())
        self.configuration.start()

    async def asyncTearDown(self):
        self.configuration.stop()
        self.directory.cleanup()

    async def execute(self, events, images=None):
        proc = type("Process", (), {})()
        proc.wait = AsyncMock(return_value=1)
        proc.stdin = FakeInput()
        proc.stdout = FakeOutput([(json.dumps(e) + "\n").encode() for e in events])
        proc.stderr = type("Stderr", (), {"read": AsyncMock(return_value=b"")})()
        stopped = AsyncMock()
        with patch.object(service.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), patch.object(service, "stop", stopped):
            response = await service.turn(service.Turn(prompt="hello", model="gemini-test", workspace=str(self.workspace), images=images or []))
            rows = []
            async for chunk in response.body_iterator:
                row = json.loads(chunk)
                if row.get("done"):
                    self.assertFalse(service.active_turns)
                    self.assertTrue(stopped.await_count)
                rows.append(row)
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

    async def test_unread_image_cannot_emit_text_or_success(self):
        image = {"mimeType": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="}
        for events in ([{"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "guess"}}],
                       [{"event": "init", "conversation_id": "thread"}, {"event": "result", "result": {"status": "SUCCESS"}}]):
            rows = await self.execute(events, images=[image])
            self.assertEqual(rows[-1]["kind"], "request")
            self.assertFalse(any("delta" in row or "done" in row for row in rows))

    def test_invalid_image_rejected_before_execution(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            service.Turn(prompt="hello", model="gemini-test", workspace=str(self.workspace),
                         images=[{"mimeType": "image/png", "data": "AAAA"}])

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

    def test_invalid_authorization_code_ends_waiting_state(self):
        login = service.Login()
        login.code_submitted = True
        login.stream.feed("OAuth error: failed to exchange authorization code for tokens")
        state = login.state()
        self.assertIn("授权失败", state["error"])
        self.assertEqual(state["stage"], "waiting")

    def test_authorization_prompt_is_not_treated_as_an_error(self):
        self.assertIsNone(service.authorization_code_error(
            "After authenticating, copy the authorization code and paste it below:"))

    def test_structured_login_steps(self):
        view = service.login_view("Select login method:\n > 1. Continue with Google Cloud\n   2. Other sign-in options")
        self.assertEqual(view["stage"], "choose")
        self.assertEqual(view["selected"], 0)
        self.assertEqual(view["options"][0]["label"], "使用 Google Cloud 企业账号登录")
        view = service.login_view("Select login method:\n > 1. Google OAuth\n   2. Use a Google Cloud project")
        self.assertEqual(view["stage"], "choose")
        self.assertEqual([option["label"] for option in view["options"]],
                         ["使用 Google OAuth 登录", "使用 Google Cloud 企业账号登录"])
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

class OAuthSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_code_and_enter_are_separate_and_duplicate_is_rejected(self):
        session = service.Login()
        session.fd = 123
        session.url = "https://accounts.google.com/o/oauth2/auth?test=1"
        session.stream.feed("Paste the authorization code below:")
        service.login = session
        writes = []
        async def settle(delay):
            self.assertEqual(writes, [b"test-code"])
        try:
            with patch.object(service.os, "write", side_effect=lambda fd, data: writes.append(data)), \
                 patch.object(service.asyncio, "sleep", side_effect=settle):
                body = service.LoginInput(session_id=session.id, action="code", code="test-code")
                await service.login_input(body)
                self.assertEqual(writes, [b"test-code", b"\r"])
                with self.assertRaises(service.HTTPException):
                    await service.login_input(body)
                self.assertNotIn("test-code", json.dumps(session.state()))
        finally:
            service.login = None

class OAuthTermsTests(unittest.IsolatedAsyncioTestCase):
    def test_checkbox_terms_take_priority_over_legacy_done(self):
        for footer in ("[Previous] [Done]", "[Previous] > Done"):
            view = service.login_view("Terms of Service & Data Use\n> [x] Yes, I agree to help\n" + footer)
            self.assertEqual(len(view["options"]), 2)
            self.assertEqual(view["selected"], 1)
            self.assertIn("不允许", view["options"][0]["label"])

    async def test_decline_data_collection_before_confirming(self):
        session = service.Login()
        session.fd = 123
        service.login = session
        checked, focus = True, "checkbox"
        writes = []
        def draw():
            session.screen.reset()
            prefix = "> " if focus == "checkbox" else ""
            footer = "[Previous] > Done" if focus == "done" else "> Previous [Done]" if focus == "previous" else "[Previous] [Done]"
            session.stream.feed("Terms of Service & Data Use\r\n" + prefix +
                                ("[x]" if checked else "[ ]") + " Yes, I agree\r\n" + footer)
        def write(fd, data):
            nonlocal checked, focus
            writes.append(data)
            if data == b"\r" and focus == "checkbox":
                checked = not checked
            elif data == b"\t":
                focus = {"checkbox": "previous", "previous": "done", "done": "checkbox"}[focus]
            draw()
        draw()
        try:
            view = session.state()
            with patch.object(service.os, "write", side_effect=write), patch.object(service.asyncio, "sleep", AsyncMock()):
                await service.login_input(service.LoginInput(session_id=session.id, action="select:0", menu_id=view["menu_id"]))
            self.assertFalse(checked)
            self.assertEqual(writes, [b"\r", b"\t", b"\t", b"\r"])
        finally:
            service.login = None

class EligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_but_ineligible_header_does_not_wait_for_project(self):
        session = service.Login()
        session.fd = 123
        session.code_submitted = True
        session.process = type("Process", (), {"returncode": None})()
        screen = ("Antigravity CLI 1.2.12\r\nuser@example.test\r\n"
                  "Eligibility Check\r\nEligibility check failed: Your current account is not eligible for Antigravity.")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(service, "ACCOUNT", Path(directory)/"account.json"), \
                 patch.object(service.os, "read", return_value=screen.encode()), \
                 patch.object(service.os, "close"), patch.object(service, "stop", AsyncMock()):
                session.task = asyncio.create_task(session.read())
                await session.task
                state = session.state()
                self.assertTrue(state["logged_in"])
                self.assertEqual(state["account"]["email"], "user@example.test")
                self.assertIn("资格检查", state["error"])
                self.assertNotIn("授权码", state["error"])

    async def test_probe_preserves_authenticated_ineligible_account(self):
        state = {"account": {"email": "user@example.test"}, "available": False, "kind": "ineligible"}
        with patch.object(service, "account", AsyncMock(return_value=state)), \
             patch.object(service.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            self.assertEqual(await service.probe(), state)
            spawn.assert_not_called()

class WorkspaceTrustTests(unittest.TestCase):
    def test_trust_prompt_is_an_actionable_choice(self):
        display = "Do you trust the contents of this project?\n> Yes, I trust this folder\nNo, exit"
        view = service.login_view(display)
        self.assertEqual(view["stage"], "choose")
        self.assertEqual(view["menu_id"], "workspace-trust")
        self.assertEqual(view["selected"], 0)
        self.assertEqual(len(view["options"]), 2)

class PersonalAccountLogoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_logout_dispatches_without_plan_and_waits_for_confirmation(self):
        service.lock = asyncio.Lock()
        proc = type("Process", (), {"returncode": None})()
        screens = [
            b"Antigravity CLI 1.2.12\r\nuser@example.test\r\n>",
            b"\x1b[2J\x1b[HAre you sure you want to sign out?",
            b"\x1b[2J\x1b[HSuccessfully logged out",
        ]
        with patch.object(service.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
             patch.object(service.os, "read", side_effect=screens), \
             patch.object(service.os, "write") as write, patch.object(service, "stop", AsyncMock()):
            await service.cli_panel("/logout")
        self.assertEqual([c.args[1] for c in write.call_args_list], [b"/logout", b"\r", b"y", b"\r"])
        self.assertFalse(service.lock.locked())

    def test_email_in_login_prompt_is_not_authenticated_header(self):
        self.assertFalse(service.signed_in_header("Sign in using user@example.test"))
        self.assertTrue(service.signed_in_header("Antigravity CLI 1.2.12\nuser@example.test\n/workspace"))

class ConcurrentTurnTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = TransportTests.asyncSetUp
    asyncTearDown = TransportTests.asyncTearDown

    async def test_nested_turn_admitted_while_parent_stream_is_open(self):
        def spawn(*args, **kwargs):
            proc = type("Process", (), {})()
            proc.wait = AsyncMock(return_value=0)
            proc.stdin = FakeInput()
            proc.stdout = FakeOutput([(json.dumps(e) + "\n").encode() for e in [
                {"event": "init", "conversation_id": "thread"},
                {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "ready"}},
                {"event": "result", "result": {"status": "SUCCESS"}},
            ]])
            proc.stderr = type("Stderr", (), {"read": AsyncMock(return_value=b"")})()
            return proc
        with patch.object(service.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn)), patch.object(service, "stop", AsyncMock()):
            parent = await service.turn(service.Turn(prompt="parent", model="gemini-test", workspace=str(self.workspace)))
            await anext(parent.body_iterator)
            service.active_turns[next(iter(service.active_turns))] = "11111111-1111-1111-1111-111111111111"
            with self.assertRaises(service.HTTPException):
                await service.turn(service.Turn(prompt="duplicate", model="gemini-test", workspace=str(self.workspace), conversation="11111111-1111-1111-1111-111111111111"))
            child = await service.turn(service.Turn(prompt="child", model="gemini-test", workspace=str(self.workspace)))
            self.assertEqual(len(service.active_turns), 2)
            with self.assertRaises(service.HTTPException):
                await service.login_start()
            rows = [json.loads(chunk) async for chunk in child.body_iterator]
            self.assertTrue(rows[-1]["done"])
            await parent.body_iterator.aclose()
            self.assertFalse(service.active_turns)
