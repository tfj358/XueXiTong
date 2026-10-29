"""2.1.1 修复验证，所有平台响应与账号均使用模拟数据。"""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

import httpx
import backend
from backend import Backend, RefreshCancelled, course_key
from superstar_checker.client import ChaoxingError, SessionExpired
import test_desktop as legacy
from test_desktop import COURSES
import release_tools as release


class UIFixTests(unittest.TestCase):
    setUp = legacy.DesktopTests.setUp
    tearDown = legacy.DesktopTests.tearDown
    drain = legacy.DesktopTests.drain
    select = legacy.DesktopTests.select

    def pump(self):
        if self.app.tick_id:
            self.root.after_cancel(self.app.tick_id)
            self.app.tick_id = None
        self.app._tick()

    def stopped_state(self):
        return dict(running=False, current="已停止", resumable=True, **COURSES[0])

    def test_resume_clears_filter_and_selects_hidden_course(self):
        self.service.state = self.stopped_state()
        self.app.search.set("其他班")
        self.app.resume_last_study()
        self.drain(lambda: not self.app.busy)
        self.assertEqual(self.app.search.get(), "")
        self.assertEqual(course_key(self.app.selected_course()), course_key(COURSES[0]))
        self.service.start.assert_called_once_with(COURSES[0])

    def test_resume_rechecks_deleted_course(self):
        self.service.state = self.stopped_state()
        self.app.update_progress()
        self.app.show_courses(COURSES[1:])
        self.app.resume_last_study()
        self.service.start.assert_not_called()
        self.assertIn("不存在", self.app.status_var.get())

    def test_resume_rechecks_running_task(self):
        self.service.state = dict(self.stopped_state(), running=True)
        self.app.resume_last_study()
        self.service.start.assert_not_called()

    def test_interval_and_reenable_reset_single_deadline(self):
        with patch("desktop.time.monotonic", return_value=100):
            self.app.auto_refresh_interval_var.set("5 分钟")
            self.app._refresh_interval_changed()
            self.assertEqual(self.app.next_sync_at, 400)
            self.app.auto_refresh_var.set(True)
            self.app._auto_refresh_setting_changed()
            self.assertEqual(self.app.next_sync_at, 400)
        with patch("desktop.time.monotonic", return_value=160):
            self.app._update_sync_label()
        self.assertIn("04:00", self.app.sync_var.get())

    def test_blocked_deadline_catches_up_once(self):
        self.app.next_sync_at = 100
        with patch("desktop.time.monotonic", return_value=200), patch.object(self.app, "refresh_courses", return_value=True) as refresh:
            for mode in ("busy", "running"):
                setattr(self.app, mode, True)
                self.app._maybe_auto_refresh()
                self.app._update_sync_label()
                self.assertIn("跳过", self.app.sync_var.get())
                setattr(self.app, mode, False)
            refresh.assert_not_called()
            self.app._maybe_auto_refresh()
            refresh.assert_called_once_with(auto=True)
        self.app.show_courses(COURSES)
        self.service.refresh_courses.reset_mock()
        self.app._maybe_auto_refresh()
        self.service.refresh_courses.assert_not_called()

    def test_refresh_duration_resets_at_finish(self):
        callback = self.app.show_courses
        with patch("desktop.time.monotonic", return_value=120):
            self.app.busy = True
            self.app.events.put(("done", "自动刷新课程", COURSES, None, callback, None, None, True, False))
            self.pump()
            self.assertEqual(self.app.next_sync_at, 180)
            self.assertIn("01:00", self.app.sync_var.get())

    def test_failed_refresh_resets_at_finish(self):
        with patch("desktop.time.monotonic", return_value=150):
            self.app.events.put(("done", "自动刷新课程", None, "模拟故障", None, None, None, True, False))
            self.pump()
        self.assertEqual(self.app.next_sync_at, 210)

    def test_logout_reason_and_retry_delay(self):
        self.service.client.has_session.return_value = False
        self.app.next_sync_at = 0
        with patch("desktop.time.monotonic", return_value=100):
            self.app._maybe_auto_refresh()
            self.app._update_sync_label()
        self.assertIn("登录", self.app.sync_var.get())
        self.assertEqual(self.app.next_sync_at, 160)
        self.service.refresh_courses.assert_not_called()

    def test_notifications_rearm_after_refresh_recovery(self):
        self.app.notification_var.set(True)
        sent = []
        # Exercise production deduplication, mock only the Windows delivery.
        fakegui = Mock(NIF_ICON=2, NIF_TIP=4, NIF_INFO=16, NIM_ADD=0, NIM_MODIFY=1)
        fakecon = Mock(IMAGE_ICON=1, LR_LOADFROMFILE=16, WM_USER=1024)
        fakegui.Shell_NotifyIcon.side_effect = lambda code, payload: sent.append(payload) if code == 1 else None
        with patch.dict("sys.modules", {"win32gui": fakegui, "win32con": fakecon}):
            for episode in range(2):
                for _ in range(4):
                    self.app.events.put(("done", "自动刷新课程", None, "网络错误", None, None, None, True, False))
                    self.pump()
                self.assertEqual(len(sent), episode + 1)
                self.app.show_courses(COURSES)
            self.app.notify_handle = None

    def test_login_success_rearms_expiry_notification(self):
        self.app.notify_seen.add("session-expired")
        with patch.object(self.app, "refresh_courses"):
            self.app.logged_in()
        self.assertNotIn("session-expired", self.app.notify_seen)

    def test_stopped_task_never_announces_complete(self):
        self.app.last_running_state = True
        self.service.state = dict(self.stopped_state(), results=[{"results": [{"status": "failed"}]}])
        with patch.object(self.app, "_notify") as notify:
            self.app.update_progress()
        self.assertEqual(notify.call_args.args[0], "学习任务已停止")

    def test_cancelled_refresh_is_not_failure(self):
        self.app.events.put(("done", "自动刷新课程", None, None, None, None, None, True, True))
        self.pump()
        self.assertEqual(self.app.auto_refresh_failures, 0)

    def test_disabling_refresh_signals_cancel(self):
        self.app.auto_refresh_var.set(False)
        self.app._auto_refresh_setting_changed()
        self.assertTrue(self.app.refresh_cancel.is_set())

    def test_auto_refresh_slow_backend_keeps_ui_responsive(self):
        released = threading.Event()
        try:
            def slow(cancel):
                released.wait(1)
                return COURSES
            self.service.refresh_courses.side_effect = slow
            self.app.refresh_courses(auto=True)
            heartbeat = []
            self.root.after(10, lambda: heartbeat.append(True))
            self.drain(lambda: bool(heartbeat))
            self.assertTrue(self.app.busy)
        finally:
            released.set()
        self.drain(lambda: not self.app.busy)
        self.assertNotIn("界面刷新失败", self.app.status_var.get())


class StatusFixTests(unittest.TestCase):
    def state(self, **changes):
        state = dict(running=False, current="全部完成", course_id=1, pending_total=1,
                     results=[{"results": [{"status": "passed"}]}])
        state.update(changes)
        with patch.object(backend.study, "load_state", return_value=state):
            return Backend.__new__(Backend).status()

    def test_last_chapter_stopped_is_resumable(self):
        result = self.state(current="已停止", results=[{"results": [{"status": "stopped"}]}])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["incomplete_chapters"], 1)
        self.assertTrue(result["resumable"])

    def test_failed_chapter_is_resumable(self):
        self.assertTrue(self.state(results=[{"results": [{"status": "failed"}]}])["resumable"])

    def test_normal_completion_is_not_resumable(self):
        self.assertFalse(self.state()["resumable"])

    def test_legacy_pending_list_is_supported(self):
        self.assertTrue(self.state(pending_total=None, pending=[1, 2])["resumable"])

    def test_manual_and_unsupported_do_not_create_resume_loop(self):
        self.assertFalse(self.state(results=[{"results": [{"status": "manual"}, {"status": "unsupported"}]}])["resumable"])

    def test_scan_exception_is_resumable_with_no_pending_count(self):
        self.assertTrue(self.state(current="异常停止", pending_total=0, results=[])["resumable"])

    def test_cancelled_before_start_does_not_read_session(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(backend.session_store, "load") as load:
            with self.assertRaises(RefreshCancelled):
                Backend.__new__(Backend).refresh_courses(cancel)
        load.assert_not_called()

    def test_refresh_uses_default_35_second_budget_and_independent_cookies(self):
        service = Backend.__new__(Backend)
        service.client = Mock(uid="test-account")
        saved = dict(uid="test-account", saved_at=1, cookies={"vc3": "mock"})
        with patch.object(backend.session_store, "load", return_value=saved), \
             patch.object(service, "_refresh_courses", new_callable=AsyncMock, return_value=COURSES) as query:
            cancel = threading.Event()
            self.assertEqual(service.refresh_courses(cancel), COURSES)
        query.assert_awaited_once_with(cancel, saved["cookies"], 35)
        service.client.request.assert_not_called()

    def test_refresh_discards_result_after_account_changes(self):
        service = Backend.__new__(Backend)
        service.client = Mock(uid="test-account")
        saved = dict(uid="test-account", saved_at=1, cookies={"vc3": "mock"})
        with patch.object(backend.session_store, "load", side_effect=[saved, dict(saved, saved_at=2)]), \
             patch.object(service, "_refresh_courses", new_callable=AsyncMock, return_value=COURSES):
            with self.assertRaisesRegex(ChaoxingError, "会话已变化"):
                service.refresh_courses(threading.Event())


class AsyncRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def query(self, handler, deadline=0.3, cancel=None, delays=(0.01, 0.01)):
        return await Backend.__new__(Backend)._refresh_courses(
            cancel or threading.Event(), {"vc3": "mock-cookie"}, deadline,
            transport=httpx.MockTransport(handler), retry_delays=delays)

    async def test_slow_response_is_cancelled_by_total_deadline(self):
        cancelled = []
        async def slow(request):
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.append(True)
        start = time.monotonic()
        with self.assertRaisesRegex(ChaoxingError, "总时限"):
            await self.query(slow, deadline=0.08)
        self.assertLess(time.monotonic()-start, 0.5)
        self.assertEqual(cancelled, [True])

    async def test_redirects_share_deadline(self):
        calls = []
        async def redirect(request):
            calls.append(str(request.url))
            await asyncio.sleep(0.04)
            return httpx.Response(302, headers={"location": "/next"})
        with self.assertRaisesRegex(ChaoxingError, "总时限"):
            await self.query(redirect, deadline=0.1)
        self.assertGreater(len(calls), 1)
        self.assertLessEqual(len(calls), 3)

    async def test_backoff_shares_deadline(self):
        calls = []
        def fail(request):
            calls.append(1)
            raise httpx.ConnectError("mock offline")
        with self.assertRaisesRegex(ChaoxingError, "总时限"):
            await self.query(fail, deadline=0.08, delays=(1, 3))
        self.assertEqual(len(calls), 1)

    async def test_network_failure_retries_at_most_twice(self):
        calls = []
        def fail(request):
            calls.append(1)
            raise httpx.ReadTimeout("mock timeout")
        with self.assertRaises(httpx.ReadTimeout):
            await self.query(fail)
        self.assertEqual(len(calls), 3)

    async def test_business_expiry_and_403_are_not_retried(self):
        for response in (httpx.Response(403), httpx.Response(200, json={"result": 0, "msg": "业务错误"}),
                         httpx.Response(302, headers={"location": "https://passport2.chaoxing.com/login"})):
            handler = Mock(return_value=response)
            with self.assertRaises(ChaoxingError):
                await self.query(handler)
            self.assertEqual(handler.call_count, 1)

    async def test_cancel_during_request(self):
        cancel = threading.Event()
        async def slow(request):
            asyncio.get_running_loop().call_later(0.02, cancel.set)
            await asyncio.sleep(5)
        start = time.monotonic()
        with self.assertRaises(RefreshCancelled):
            await self.query(slow, cancel=cancel)
        self.assertLess(time.monotonic() - start, 0.3)

    async def test_cancel_during_backoff_stops_retry(self):
        cancel = threading.Event()
        calls = []
        def fail(request):
            calls.append(1)
            asyncio.get_running_loop().call_later(0.02, cancel.set)
            raise httpx.ConnectError("mock")
        with self.assertRaises(RefreshCancelled):
            await self.query(fail, cancel=cancel, delays=(1, 3))
        self.assertEqual(calls, [1])

    async def test_success_parses_in_isolated_session(self):
        def ok(request):
            self.assertIn("vc3=mock-cookie", request.headers["cookie"])
            return httpx.Response(200, json={"result": 1})
        with patch.object(backend.decode, "parse_courses", return_value=COURSES):
            self.assertEqual(await self.query(ok), COURSES)

    async def test_untrusted_redirect_is_rejected(self):
        handler = Mock(return_value=httpx.Response(302, headers={"location": "https://example.com/"}))
        with self.assertRaisesRegex(ChaoxingError, "跳转"):
            await self.query(handler)
        self.assertEqual(handler.call_count, 1)


class ReleaseTests(unittest.TestCase):
    def test_full_pipeline_repeated_build_and_failed_build_preserve_history(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outputs = root / "outputs"
            source = outputs / "desktop-source"
            shutil.copytree(Path(__file__).parent, source,
                            ignore=shutil.ignore_patterns("__pycache__"))
            upstream = outputs / "superstar-checker"
            (upstream / "src").mkdir(parents=True)
            (upstream / "tests").mkdir()
            for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml"):
                (upstream / name).write_text("mock")
            (outputs / "桌面版使用说明.md").write_text("2.1.1")
            (outputs / "第三方声明").mkdir()
            (outputs / "第三方声明" / "LICENSE.txt").write_text("mock license")
            calls = []
            def runner(command, log, **kwargs):
                Path(log).write_text("mock build validation")
                calls.append(command)
                if "PyInstaller" in command:
                    dist = Path(command[command.index("--distpath") + 1])
                    dist.mkdir()
                    (dist / "学习通助手.exe").write_bytes(b"mock executable")
                if "--check-install" in command:
                    release.write_json(command[-1], dict(app_version="2.1.1", frozen=True,
                        offline=True, aes_ok=True, des_ok=True, notifications_ok=True, font_table_entries=1))
                if "--smoke-test" in command:
                    release.write_json(command[-1], dict(frozen=True, mock_backend=True,
                        window_visible=True, closed=True, errors=[], course_count=1))
            pe = dict(FileVersionMS=(2 << 16) + 1, FileVersionLS=(1 << 16))
            with patch("win32api.GetFileVersionInfo", return_value=pe):
                first = release.build(source, runner=runner)
                baseline = release.manifest(first)
                second = release.build(source, runner=runner)
                self.assertNotEqual(first, second)
                self.assertEqual(release.manifest(first), baseline)
                self.assertEqual(baseline, json.loads((first / "SHA256.json").read_text(encoding="utf-8")))
                def failed(command, log, **kwargs):
                    runner(command, log, **kwargs)
                    if "PyInstaller" in command:
                        raise RuntimeError("intentional compiler failure")
                with self.assertRaisesRegex(RuntimeError, "compiler failure"):
                    release.build(source, runner=failed)
                self.assertEqual(release.manifest(first), baseline)
                self.assertTrue((second / "source.zip").exists())

    def test_repeated_publish_and_unique_stage_preserve_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = release.unique_dir(root / "staging", "same")
            (first / "old.exe").write_bytes(b"history")
            published = release.publish(first, root / "releases")
            baseline = release.manifest(published)
            second = release.unique_dir(root / "staging", "same")
            (second / "new.exe").write_bytes(b"new")
            other = release.publish(second, root / "releases")
            self.assertNotEqual(published, other)
            self.assertEqual(release.manifest(published), baseline)
            a = release.unique_dir(root / "staging", "collision")
            b = release.unique_dir(root / "staging", "collision")
            self.assertNotEqual(a, b)

    def test_failed_build_retains_logs_and_existing_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "outputs" / "desktop-source"
            source.mkdir(parents=True)
            (source / "version.txt").write_text("2.1.1")
            history = root / "outputs" / "release" / "2.1.1" / "old"
            history.mkdir(parents=True)
            (history / "old.exe").write_bytes(b"history")
            baseline = release.manifest(history)
            def fail(command, log, **kwargs):
                Path(log).write_text("intentional test failure")
                raise RuntimeError("simulated failed build")
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                release.build(source, runner=fail)
            self.assertEqual(release.manifest(history), baseline)
            failed = list((root / "outputs" / "release" / ".failed" / "2.1.1").iterdir())
            self.assertEqual(len(failed), 1)
            self.assertTrue((failed[0] / "tests.log").exists())

    def test_version_resource_is_generated_from_single_source(self):
        resource = release.version_resource("2.1.1", "sample.exe")
        self.assertIn("filevers=(2, 1, 1, 0)", resource)
        self.assertIn("'ProductVersion', '2.1.1'", resource)
        with self.assertRaises(ValueError):
            release.version_resource("invalid", "sample.exe")

    def test_snapshot_excludes_personal_sessions_and_build_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, upstream = root / "desktop-source", root / "upstream"
            source.mkdir()
            (upstream / "src").mkdir(parents=True)
            (upstream / "tests").mkdir()
            for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml"):
                (upstream / name).write_text("mock source")
            for name in ("desktop.py", "version.txt", "session.json", "desktop.log"):
                (source / name).write_text("mock")
            (upstream / "session.json").write_text("private")
            release.source_snapshot(source, upstream, root / "source.zip")
            with zipfile.ZipFile(root / "source.zip") as archive:
                self.assertIn("desktop-source/desktop.py", archive.namelist())
                self.assertFalse(any("session.json" in n or n.endswith(".log") for n in archive.namelist()))
