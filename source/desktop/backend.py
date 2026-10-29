"""桌面端适配层。仅复用上游 HTTP 模块，不启动 MCP 或外部 Python。"""

import asyncio
import re
import threading
from urllib.parse import urljoin, urlparse

import httpx

from bs4 import BeautifulSoup
from superstar_checker import decode, materials, notice, qrlogin, session_store, study, work
from superstar_checker.client import (ChaoxingClient, ChaoxingError, DEFAULT_HEADERS,
                                      CaptchaRequired, SessionExpired, is_transient_network_error)


class RefreshCancelled(ChaoxingError):
    """刷新主动取消，不作为网络故障计数。"""


def course_key(course):
    return tuple(str(course.get(key, "")) for key in ("course_id", "class_id", "cpi"))


def safe_url(url):
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("这条记录没有可打开的网页链接。")
    return str(url)


def plain_text(value):
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)


class Backend:
    def __init__(self):
        self.client = ChaoxingClient()
        self.manager = study.MANAGER
        self.quiz_cache = {}

    def courses(self, timeout=None):
        self.client.ensure_login()
        request = {"params": {"view": "json", "rss": "1"}}
        if timeout is not None:
            request["timeout"] = timeout
        response = self.client.follow("https://mooc1-api.chaoxing.com/mycourse/backclazzdata", **request)
        body = response.json()
        if body.get("result") != 1:
            raise ChaoxingError(str(body.get("msg") or "课程列表读取失败，请重新登录。"))
        return decode.parse_courses(body)

    def chapters(self, course):
        self.client.ensure_login()
        return study.fetch_chapters(self.client, *course_key(course))

    def refresh_courses(self, cancel_event, deadline=35):
        """独立会话执行只读刷新；总预算包含跳转和重试，关闭不影响学习会话。"""
        if cancel_event.is_set():
            raise RefreshCancelled("自动刷新已取消")
        saved = session_store.load()
        if not isinstance(saved, dict) or not saved.get("uid") or not isinstance(saved.get("cookies"), dict) or not ({"vc3", "_d"} & saved["cookies"].keys()):
            raise SessionExpired()
        stamp = (saved.get("uid"), saved.get("saved_at"))
        if str(saved["uid"]) != str(self.client.uid):
            raise ChaoxingError("本机登录账号已变化，请手动刷新课程。")
        rows = asyncio.run(self._refresh_courses(cancel_event, saved["cookies"], deadline))
        latest = session_store.load() or {}
        if (latest.get("uid"), latest.get("saved_at")) != stamp:
            raise ChaoxingError("刷新期间登录会话已变化，请重新读取课程。")
        return rows

    async def _refresh_courses(self, cancel_event, cookies, deadline=35, *,
                               transport=None, retry_delays=(1, 3)):
        async def query():
            jar = httpx.Cookies()
            for name, value in cookies.items():
                jar.set(name, value, domain=".chaoxing.com", path="/")
            async with httpx.AsyncClient(headers=DEFAULT_HEADERS, cookies=jar,
                                         timeout=10, follow_redirects=False,
                                         transport=transport) as client:
                for attempt in range(3):
                    try:
                        url = "https://mooc1-api.chaoxing.com/mycourse/backclazzdata"
                        params = {"view": "json", "rss": "1"}
                        for hop in range(7):
                            response = await client.get(url, params=params)
                            if response.status_code == 403:
                                raise CaptchaRequired("HTTP 403")
                            if response.status_code not in (301, 302, 303, 307, 308):
                                break
                            location = response.headers.get("location")
                            if not location or hop == 6:
                                raise ChaoxingError("课程接口跳转异常，请手动刷新。")
                            url = urljoin(str(response.url), location)
                            host = urlparse(url).hostname or ""
                            if host == "passport2.chaoxing.com":
                                raise SessionExpired()
                            if not host.endswith(".chaoxing.com") or urlparse(url).scheme != "https":
                                raise ChaoxingError("课程接口返回了无法识别的跳转。")
                            params = None
                        response.raise_for_status()
                        body = response.json()
                        if body.get("result") != 1:
                            raise ChaoxingError(str(body.get("msg") or "课程列表读取失败，请重新登录。"))
                        return decode.parse_courses(body)
                    except Exception as exc:
                        if attempt == 2 or not is_transient_network_error(exc):
                            raise
                        await asyncio.sleep(retry_delays[attempt])

        async def watch_cancel():
            while not cancel_event.is_set():
                await asyncio.sleep(0.05)

        tasks = []
        try:
            async with asyncio.timeout(deadline):
                tasks = [asyncio.create_task(query()), asyncio.create_task(watch_cancel())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if tasks[1] in done or cancel_event.is_set():
                    raise RefreshCancelled("自动刷新已取消")
                return await tasks[0]
        except TimeoutError as exc:
            raise ChaoxingError(f"自动刷新已达到 {deadline:g} 秒总时限，请检查网络。") from exc
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def status(self):
        loaded = study.load_state()
        state = loaded if isinstance(loaded, dict) else {"running": False}
        if state.get("running") and not study._process_alive(state.get("pid")):
            state = dict(state, running=False, current="任务进程已退出，可重新开始")
        results = state.get("results")
        if not isinstance(results, list):
            results = []
        try:
            pending_total = int(state.get("pending_total") or 0)
        except (TypeError, ValueError):
            pending_total = 0
        # 旧版状态可能只有 pending 列表，没有 pending_total；按列表长度
        # 推导，保证升级后仍能显示手动恢复入口。
        if pending_total <= 0 and isinstance(state.get("pending"), list):
            pending_total = len(state["pending"])
        state = dict(state)
        results = [row for row in results if isinstance(row, dict)]
        state["results"] = results
        state["pending_total"] = pending_total
        state["processed"] = len(results)
        def retryable(row):
            items = row.get("results") or []
            items = items if isinstance(items, list) else []
            return any(isinstance(item, dict) and item.get("status") in {"failed", "stopped"}
                       for item in items)
        state["incomplete_chapters"] = sum(retryable(row) for row in results)
        abnormal = bool(state.get("error")) or state.get("current") in {"异常停止", "任务进程已退出，可重新开始"}
        state["resumable"] = bool(
            not state.get("running")
            and (pending_total > len(results) or state["incomplete_chapters"] or abnormal)
            and state.get("course_id") not in (None, "")
        )
        return state

    def start(self, course):
        self.client.ensure_login()
        current = self.status()
        if current.get("running"):
            raise ChaoxingError("已有课程正在运行，请先停止当前任务。")
        with self.manager._lock:
            if not (self.manager._thread and self.manager._thread.is_alive()):
                self.manager._state = {"running": False}
        result = self.manager.start(self.client, *course_key(course))
        if result.get("status") != "started":
            raise ChaoxingError(result.get("detail") or "任务未能启动。")
        return result

    def stop(self):
        # 跨进程只写停止标志，避免以过期内存覆盖另一进程的实时状态。
        study.FileStopEvent().set()

    def read_chapter(self, course, chapter):
        self.client.ensure_login()
        args = course_key(course)
        atts, defaults = study.fetch_card_attachments(
            self.client, *args, str(chapter["knowledge_id"])
        )
        docs = [att for att in atts if decode.attachment_kind(att) in {"document", "read"}]
        return study.study_attachments(
            self.client, *args, str(chapter["knowledge_id"]), docs, defaults
        )

    def homeworks(self, course):
        self.client.ensure_login()
        return work.list_homeworks(self.client, *course_key(course))

    def quizzes(self, course, batch=5):
        """记录没有测验的已扫描章节，确保“继续扫描”可以前进。"""
        self.client.ensure_login()
        key = (str(self.client.uid), *course_key(course))
        cache = self.quiz_cache.setdefault(key, {"seen": set(), "items": []})
        chapters = [c for c in self.chapters(course) if c.get("job_count", 0) > 0]
        todo = [c for c in chapters if c["knowledge_id"] not in cache["seen"]]
        words = ("测验", "测试", "练习", "作业", "quiz", "test")
        todo.sort(key=lambda c: 0 if any(w in c["title"].lower() for w in words) else 1)
        scanned = 0
        for chapter in todo[:batch]:
            attachments, _ = study.fetch_card_attachments(
                self.client, *course_key(course), str(chapter["knowledge_id"])
            )
            for att in attachments:
                if decode.attachment_kind(att) == "work" or str(att.get("jobid", "")).startswith("work-"):
                    prop = att.get("property") or {}
                    cache["items"].append({
                        "knowledge_id": str(chapter["knowledge_id"]),
                        "chapter": chapter["title"],
                        "name": prop.get("name") or prop.get("title") or chapter["title"],
                        "jobid": att.get("jobid"), "isPassed": att.get("isPassed"),
                    })
            cache["seen"].add(chapter["knowledge_id"])
            scanned += 1
        return {"items": list(cache["items"]), "scanned": len(cache["seen"]),
                "total": len(chapters), "batch": scanned}

    def questions(self, course, row):
        self.client.ensure_login()
        if row["kind"] == "quiz":
            result = work.fetch_quiz_questions(
                self.client, *course_key(course), row["knowledge_id"], row["jobid"]
            )
            return row["jobid"], result["questions"]
        result = work.fetch_homework(self.client, str(row["work_id"]), row.get("detail_url", ""))
        return str(row["work_id"]), result["questions"]

    def save_answers(self, work_id, answers):
        self.client.ensure_login()
        result = work.save_homework(self.client, work_id, answers)
        if not result.get("status"):
            raise ChaoxingError("平台未确认暂存成功，请在学习通中检查，或稍后重试。")
        return result

    def score(self, course, row):
        self.client.ensure_login()
        work_id = row.get("work_id") or str(row.get("jobid", "")).removeprefix("work-")
        result = work.get_homework_score(self.client, *course_key(course), str(work_id))
        if result.get("error"):
            raise ChaoxingError(result["error"])
        return result

    def materials(self, course, limit):
        self.client.ensure_login()
        return materials.list_materials(self.client, *course_key(course), max_chapters=limit)

    def folders(self):
        self.client.ensure_login()
        return notice.list_folders(self.client)

    def notices(self, folder="", keyword="", unread=False):
        self.client.ensure_login()
        return notice.list_notices(self.client, folder_uuid=folder,
                                   keyword=keyword, limit=50, unread_only=unread)

    def _can_change_account(self):
        if self.status().get("running"):
            raise ChaoxingError("请先停止当前学习任务，再切换或退出账号。")

    def password_login(self, phone, password):
        self._can_change_account()
        candidate = ChaoxingClient()
        try:
            result = candidate.login(phone, password)
            self.client._reload_from_store()
            self.quiz_cache.clear()
            return result
        finally:
            candidate._client.close()

    def logout(self):
        self._can_change_account()
        self.client.clear_session()
        self.quiz_cache.clear()

    def login_qr(self, update, cancel: threading.Event):
        self._can_change_account()
        candidate = ChaoxingClient()
        try:
            page = candidate.reset_for_login()
            soup = BeautifulSoup(page.text, "html.parser")
            uuid = (soup.find("input", id="uuid") or {}).get("value", "")
            enc = (soup.find("input", id="enc") or {}).get("value", "")
            for value in (uuid, enc):
                if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
                    raise ChaoxingError("登录页暂时无法生成二维码，请尝试密码登录。")
            for round_no in range(1, 5):
                if cancel.is_set():
                    raise ChaoxingError("扫码已取消，原会话保留。")
                png = qrlogin._new_code(candidate, uuid)
                update(png, "用学习通 App 扫码", f"第 {round_no}/4 张码 · 过期后自动更新")
                for attempt in range(50):
                    if cancel.wait(3):
                        raise ChaoxingError("扫码已取消，原会话保留。")
                    body = qrlogin._poll(candidate, uuid, enc)
                    code = str(body.get("type", ""))
                    if str(body.get("status", "")).lower() == "true":
                        if body.get("containTwoFactorLogin"):
                            raise ChaoxingError("账号需要二次验证，请改用密码登录。")
                        if cancel.is_set():
                            raise ChaoxingError("扫码已取消，原会话保留。")
                        result = candidate.finalize_login("QR")
                        self.client._reload_from_store()
                        self.quiz_cache.clear()
                        return result
                    if code == "4":
                        update(None, "已扫码，请在手机确认", f"本张码约剩 {(49-attempt)*3} 秒")
                    elif code in qrlogin.DEAD_TYPE_TEXT:
                        if code != "2":
                            raise ChaoxingError(qrlogin.DEAD_TYPE_TEXT[code])
                        break
                    elif code not in ("", "3"):
                        raise ChaoxingError("扫码状态异常，请重新获取二维码。")
                if round_no < 4:
                    fresh = candidate.request("POST", qrlogin.REFRESH,
                                              headers={"Referer": candidate.LOGIN_PAGE})
                    fresh.raise_for_status()
                    body = fresh.json()
                    uuid, enc = body["uuid"], body["enc"]
            raise ChaoxingError("二维码已超时，请重新点击扫码登录。")
        except Exception:
            candidate.restore_login_state()
            raise
        finally:
            candidate._client.close()
