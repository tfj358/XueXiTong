"""桌面版回归检查；使用模拟数据，不启动真实课程或更改真实账号。"""

import threading
import time
import tkinter as tk
import unittest
import tempfile
import json
import os
import sys
from types import SimpleNamespace
import httpx
from pathlib import Path
from unittest.mock import Mock, patch

import backend
from backend import Backend, course_key, safe_url
from desktop import App
from superstar_checker.client import CaptchaRequired, ChaoxingError, SessionExpired

COURSES = [
    {"course_id": 1, "class_id": 10, "cpi": 100, "name": "测试课程", "teacher": "教师甲", "active": True},
    {"course_id": 1, "class_id": 20, "cpi": 100, "name": "测试课程其他班", "teacher": "教师乙", "active": True},
]


class FakeBackend:
    def __init__(self):
        self.client = Mock(uid="test-account")
        self.client.has_session.return_value = True
        self.courses = Mock(return_value=COURSES)
        self.refresh_courses = Mock(return_value=COURSES)
        self.state = {"running": False}
        self.stops = 0
        self.start = Mock(return_value={"status": "started"})

    def status(self):
        return self.state

    def stop(self):
        self.stops += 1


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.service = FakeBackend()
        self.settings_dir = tempfile.TemporaryDirectory()
        self.app = App(self.root, self.service, autoload=False,
                       settings_path=Path(self.settings_dir.name) / "desktop-settings.json")
        self.app.notification_var.set(False)
        self.app.show_courses(COURSES)
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        self.app.destroy()
        self.settings_dir.cleanup()

    def drain(self, predicate, timeout=3):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(predicate(), "UI operation timed out")

    def blocker(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def select(self, course):
        self.app.course_table.tree.selection_set(":".join(course_key(course)))
        self.app.on_course_selected()

    def test_same_course_different_class_selection_survives_refresh(self):
        self.select(COURSES[1])
        self.app.show_courses(COURSES)
        self.assertEqual(course_key(self.app.selected_course()), course_key(COURSES[1]))

    def test_auto_refresh_runs_in_background_and_preserves_selection(self):
        self.select(COURSES[1])
        self.app.auto_refresh_interval = 60
        self.app.next_sync_at = time.monotonic() - 1
        self.app._maybe_auto_refresh()
        self.drain(lambda: not self.app.busy)
        self.service.refresh_courses.assert_called_once_with(self.app.refresh_cancel)
        self.assertEqual(course_key(self.app.selected_course()), course_key(COURSES[1]))

    def test_auto_refresh_skips_busy_operation(self):
        self.app.auto_refresh_interval = 0
        self.app.next_sync_at = 0
        self.app.busy = True
        self.app._maybe_auto_refresh()
        self.service.refresh_courses.assert_not_called()

    def test_auto_refresh_skips_running_study(self):
        self.app.auto_refresh_interval = 0
        self.app.next_sync_at = 0
        self.app.running = True
        self.app._maybe_auto_refresh()
        self.service.refresh_courses.assert_not_called()
        self.assertIn("学习任务运行中", self.app.status_var.get())

    def test_retry_repeats_transient_failure_in_background(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("temporary network error")
            return "ok"
        self.app._run("重试检查", flaky, retries=1, silent=True)
        self.drain(lambda: not self.app.busy)
        self.assertEqual(len(calls), 2)
        self.assertIn("重试检查完成", self.app.status_var.get())

    def test_non_transient_failures_are_not_retried(self):
        for error in (CaptchaRequired("test"), SessionExpired(), ChaoxingError("业务错误"),
                      ValueError("解析错误")):
            with self.subTest(error=type(error).__name__):
                calls = []

                def failed():
                    calls.append(1)
                    raise error

                self.app._run("不可重试检查", failed, retries=2, silent=True)
                self.drain(lambda: not self.app.busy)
                self.assertEqual(calls, [1])

    def test_transient_failures_are_limited_to_requested_retries(self):
        calls = []

        def failed():
            calls.append(1)
            raise httpx.ConnectTimeout("temporary network error")

        self.app._run("限次检查", failed, retries=2, silent=True)
        self.drain(lambda: not self.app.busy, timeout=8)
        self.assertEqual(len(calls), 3)
        self.assertIn("temporary network error", self.app.status_var.get())

    def test_refresh_interval_is_saved(self):
        self.app.auto_refresh_interval_var.set("5 分钟")
        self.app._refresh_interval_changed()
        self.assertEqual(self.app.auto_refresh_interval, 300)
        saved = json.loads(self.app.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_interval"], 300)

    def test_settings_are_saved_atomically_with_selection(self):
        self.select(COURSES[1])
        self.app.search.set("测试")
        self.app._save_settings()
        saved = json.loads(self.app.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["search"], "测试")
        self.assertEqual(saved["course_key"], ":".join(course_key(COURSES[1])))
        self.assertTrue(saved["geometry"])
        self.assertFalse(list(self.settings_dir.name and Path(self.settings_dir.name).glob("*.tmp")))

    def test_corrupt_settings_are_backed_up_and_reset(self):
        corrupt = self.app.settings_path
        corrupt.write_text("{broken", encoding="utf-8")
        loaded = self.app._load_settings()
        self.assertEqual(loaded, {})
        backups = list(corrupt.parent.glob("desktop-settings.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{broken")

    def test_resume_candidate_requires_matching_existing_course(self):
        self.service.state = {
            "running": False, "current": "已停止", "course_id": 1, "class_id": 10,
            "cpi": 100, "pending_total": 3, "results": [{"chapter": "第一章"}],
            "resumable": True,
        }
        self.app.update_progress()
        self.assertEqual(course_key(self.app.resume_course), course_key(COURSES[0]))
        self.assertEqual(str(self.app.resume_button["state"]), "normal")
        self.service.state = dict(self.service.state, course_id=999)
        self.app.update_progress()
        self.assertIsNone(self.app.resume_course)
        self.assertEqual(str(self.app.resume_button["state"]), "disabled")
        self.assertIn("找不到", self.app.resume_hint.get())

    def test_resume_only_starts_after_button_click(self):
        self.service.state = {
            "running": False, "current": "异常停止", "course_id": 1, "class_id": 10,
            "cpi": 100, "pending_total": 2, "results": [{"chapter": "第一章"}],
            "resumable": True,
        }
        self.app.update_progress()
        self.service.start.reset_mock()
        self.assertEqual(self.service.start.call_count, 0)
        self.app.resume_button.invoke()
        self.drain(lambda: not self.app.busy)
        self.service.start.assert_called_once_with(COURSES[0])

    def test_windows_notification_is_deduplicated(self):
        self.app.notification_var.set(True)
        win32con = SimpleNamespace(
            IMAGE_ICON=1, LR_LOADFROMFILE=2, WM_USER=1024,
            NIF_ICON=4, NIF_TIP=8, NIF_INFO=16, NIM_ADD=20, NIM_MODIFY=21,
            NIM_DELETE=22)
        win32gui = SimpleNamespace(
            NIF_ICON=4, NIF_TIP=8, NIF_INFO=16, NIM_ADD=20, NIM_MODIFY=21, NIM_DELETE=22,
            LoadImage=Mock(return_value=object()), Shell_NotifyIcon=Mock())
        with patch.dict(sys.modules, {"win32con": win32con, "win32gui": win32gui}):
            self.app._notify("标题", "消息", "same-key")
            self.app._notify("标题", "消息", "same-key")
            self.app.notify_handle = None
        self.assertEqual(win32gui.Shell_NotifyIcon.call_count, 2)

    def test_windows_notification_can_be_disabled(self):
        self.app.notification_var.set(False)
        with patch.dict(sys.modules, {"win32gui": Mock(), "win32con": Mock()}):
            self.app._notify("标题", "消息", "disabled-key")
        self.assertNotIn("disabled-key", self.app.notify_seen)

    def test_close_handles_status_read_failure(self):
        self.service.status = Mock(side_effect=RuntimeError("state unavailable"))
        with patch("desktop.messagebox.askyesno", return_value=True) as ask, \
             patch.object(self.app.root, "after") as after, \
             patch.object(self.app, "destroy") as destroy:
            self.app.close()
        ask.assert_called_once()
        destroy.assert_not_called()
        after.assert_called_once()

    def test_close_handles_stop_failure_with_force_exit(self):
        self.service.state = {"running": True}
        self.service.stop = Mock(side_effect=RuntimeError("stop unavailable"))
        with patch("desktop.messagebox.askyesno", side_effect=[True, False]) as ask, \
             patch.object(self.app, "destroy") as destroy:
            self.app.close()
        self.assertEqual(ask.call_count, 2)
        destroy.assert_called_once()

    def test_wait_to_close_handles_status_read_failure(self):
        self.service.status = Mock(side_effect=RuntimeError("state unavailable"))
        with patch("desktop.messagebox.askyesno", return_value=False), \
             patch.object(self.app, "destroy") as destroy:
            self.app._wait_to_close(time.monotonic() + 1)
        destroy.assert_called_once()

    def test_course_switch_discards_late_network_result(self):
        event, callback = self.blocker(), Mock()
        self.app._run("读取测试", lambda: event.wait(2), callback, course=COURSES[0])
        self.select(COURSES[1])
        event.set()
        self.drain(lambda: not self.app.busy)
        callback.assert_not_called()
        self.assertIn("课程已切换", self.app.status_var.get())

    def test_stop_remains_available_while_query_is_busy(self):
        self.service.state = {"running": True, "current": "模拟学习中"}
        self.app.update_progress()
        event = self.blocker()
        self.app._run("耗时查询", lambda: event.wait(2))
        self.assertEqual(str(self.app.stop_button["state"]), "normal")
        self.app.stop_button.invoke()
        self.assertEqual(self.service.stops, 1)
        self.assertTrue(self.app.busy)
        event.set()
        self.drain(lambda: not self.app.busy)

    def test_network_runs_off_ui_thread_and_callback_on_ui_thread(self):
        ui_thread = threading.get_ident()
        callback_threads, worker_threads = [], []
        def query():
            worker_threads.append(threading.get_ident())
        self.app._run("线程检查", query,
                      lambda _: callback_threads.append(threading.get_ident()))
        self.drain(lambda: bool(callback_threads))
        self.assertNotEqual(worker_threads[0], ui_thread)
        self.assertEqual(callback_threads[0], ui_thread)

    def test_search_no_results_clears_selected_course(self):
        self.app.search.set("不存在的课程")
        self.assertIsNone(self.app.selected_course())
        self.assertEqual(str(self.app.start_button["state"]), "disabled")


class BackendTests(unittest.TestCase):
    def test_status_marks_incomplete_stopped_state_resumable(self):
        service = Backend.__new__(Backend)
        state = {
            "running": False, "current": "异常停止", "course_id": 1, "class_id": 10,
            "cpi": 100, "pending_total": 4, "results": [{"chapter": "第一章"}],
        }
        with patch.object(backend.study, "load_state", return_value=state):
            result = service.status()
        self.assertEqual(result["processed"], 1)
        self.assertTrue(result["resumable"])

    def test_status_keeps_legacy_completed_state_non_resumable(self):
        service = Backend.__new__(Backend)
        state = {"running": False, "current": "全部完成", "results": []}
        with patch.object(backend.study, "load_state", return_value=state):
            result = service.status()
        self.assertEqual(result["processed"], 0)
        self.assertFalse(result["resumable"])

    def test_status_tolerates_corrupt_progress_fields(self):
        service = Backend.__new__(Backend)
        state = {"running": False, "current": "已停止", "pending_total": "broken",
                 "results": {"unexpected": True}}
        with patch.object(backend.study, "load_state", return_value=state):
            result = service.status()
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["results"], [])
        self.assertFalse(result["resumable"])

    def test_status_turns_dead_process_into_resumable_state(self):
        service = Backend.__new__(Backend)
        state = {
            "running": True, "pid": 123, "current": "学习中", "course_id": 1,
            "class_id": 10, "cpi": 100, "pending_total": 2,
            "results": [{"chapter": "第一章"}],
        }
        with patch.object(backend.study, "load_state", return_value=state), \
             patch.object(backend.study, "_process_alive", return_value=False):
            result = service.status()
        self.assertFalse(result["running"])
        self.assertEqual(result["current"], "任务进程已退出，可重新开始")
        self.assertTrue(result["resumable"])

    def test_quiz_scan_advances_past_chapters_with_no_quiz(self):
        service = Backend.__new__(Backend)
        service.client = Mock(uid="test")
        service.quiz_cache = {}
        service.chapters = lambda _: [
            {"knowledge_id": "a", "title": "第一章", "job_count": 1},
            {"knowledge_id": "b", "title": "第二章", "job_count": 1},
        ]
        with patch.object(backend.study, "fetch_card_attachments", return_value=([], {})) as fetch:
            first = service.quizzes(COURSES[0], batch=1)
            second = service.quizzes(COURSES[0], batch=1)
        self.assertEqual((first["scanned"], second["scanned"]), (1, 2))
        self.assertEqual([call.args[-1] for call in fetch.call_args_list], ["a", "b"])

    def test_canceled_qr_preserves_existing_session(self):
        service = Backend.__new__(Backend)
        service.status = lambda: {"running": False}
        candidate = Mock()
        value = "a"*32
        candidate.reset_for_login.return_value.text = f'<input id="uuid" value="{value}"><input id="enc" value="{value}">'
        cancel = threading.Event()
        cancel.set()
        with patch.object(backend, "ChaoxingClient", return_value=candidate):
            with self.assertRaisesRegex(Exception, "扫码已取消"):
                service.login_qr(Mock(), cancel)
        candidate.finalize_login.assert_not_called()
        candidate.restore_login_state.assert_called_once()
        candidate._client.close.assert_called_once()

    def test_account_switch_refused_during_learning(self):
        service = Backend.__new__(Backend)
        service.status = lambda: {"running": True}
        with patch.object(backend, "ChaoxingClient") as factory:
            with self.assertRaisesRegex(Exception, "请先停止"):
                service.password_login("test", "not-a-real-password")
        factory.assert_not_called()

    def test_resource_links_reject_non_web_schemes(self):
        for url in ("javascript:alert(1)", "file:///C:/secret", "data:text/html,test", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_url(url)
        self.assertEqual(safe_url("https://example.com/course.pdf"), "https://example.com/course.pdf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
