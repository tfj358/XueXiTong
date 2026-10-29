"""Superstar Checker 本地桌面界面，可由 PyInstaller 打包为独立 EXE。"""

import argparse
import base64
import ctypes
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import webbrowser

# 在导入会加载任务状态的上游模块之前隔离自检数据目录。
_diagnostic_directory = None
if __name__ == "__main__" and any(flag in sys.argv[1:] for flag in ("--check-install", "--smoke-test")):
    import tempfile
    _diagnostic_directory = tempfile.TemporaryDirectory(prefix="superstar-diagnostic-")
    os.environ["SUPERSTAR_DATA_DIR"] = _diagnostic_directory.name

from backend import Backend, RefreshCancelled, course_key, plain_text, safe_url
from superstar_checker.client import SessionExpired, is_transient_network_error

LOG = logging.getLogger("superstar-desktop")
BG, INK, MUTED, ACCENT = "#f2f5fa", "#172b42", "#62758a", "#087f8c"
_VERSION_FILE = Path(__file__).with_name("version.txt")
try:
    APP_VERSION = _VERSION_FILE.read_text(encoding="utf-8").strip()
except OSError as exc:
    raise RuntimeError(f"版本文件缺失：{_VERSION_FILE}") from exc
if not APP_VERSION:
    raise RuntimeError(f"版本文件为空：{_VERSION_FILE}")


def duration(seconds):
    seconds = max(0, int(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class Table(ttk.Frame):
    def __init__(self, parent, columns, height=10):
        super().__init__(parent)
        self.rows = {}
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns],
                                 show="headings", selectmode="browse", height=height)
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        for name, label, width in columns:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=50, stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def selected(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def fill(self, rows, values, key=None, select_first=False):
        selected = self.tree.selection()
        previous = selected[0] if selected else None
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for index, row in enumerate(rows):
            iid = str(key(row) if key else index)
            self.rows[iid] = row
            self.tree.insert("", "end", iid=iid, values=values(row))
        selected = previous if previous in self.rows else (next(iter(self.rows), None) if select_first else None)
        if selected is not None:
            self.tree.selection_set(selected)
            self.tree.see(selected)


class App:
    def __init__(self, root, backend=None, autoload=True, settings_path=None):
        self.root = root
        self.backend = backend or Backend()
        self.settings_path = Path(settings_path) if settings_path else Path.home() / ".superstar-checker" / "desktop-settings.json"
        self.settings = self._load_settings()
        self.events = queue.Queue()
        self.busy = False
        self.closing = False
        self.buttons = []
        self.all_courses = []
        self.homework_rows = []
        self.quiz_rows = []
        self.last_course_key = None
        self.work_windows = []
        self.qr_window = None
        self.qr_cancel = None
        self.running = False
        self.stop_pending = False
        self.resume_course = None
        self.awake = False
        self.last_poll = 0
        self.last_signature = None
        self.auto_refresh_var = tk.BooleanVar(value=bool(self.settings.get("auto_refresh", True)))
        self.notification_var = tk.BooleanVar(value=bool(self.settings.get("notifications", True)))
        try:
            saved_interval = int(self.settings.get("refresh_interval", 60) or 60)
        except (TypeError, ValueError):
            saved_interval = 60
        self.auto_refresh_interval_var = tk.StringVar(
            value={60: "1 分钟", 300: "5 分钟", 600: "10 分钟", 1800: "30 分钟"}.get(
                saved_interval, "1 分钟"))
        self.auto_refresh_interval = self._refresh_seconds()
        self.refresh_cancel = threading.Event()
        self.auto_refresh_pause_noticed = False
        self.auto_refresh_inflight = False
        self.auto_refresh_failures = 0
        self.auto_refresh_failure_notified = False
        self.last_sync_at = None
        self.next_sync_at = time.monotonic() + self.auto_refresh_interval
        self.sync_reason = "尚未同步"
        self.last_running_state = False
        self.notify_seen = set()
        self.notify_handle = None
        self.restore_course_key = self.settings.get("course_key")
        self.tick_id = None
        root.title("学习通助手 · 本地桌面版")
        root.configure(bg=BG)
        width = min(1280, root.winfo_screenwidth()-60)
        height = min(850, root.winfo_screenheight()-90)
        saved_geometry = self.settings.get("geometry")
        try:
            root.geometry(saved_geometry if isinstance(saved_geometry, str) and saved_geometry else f"{width}x{height}")
        except tk.TclError:
            root.geometry(f"{width}x{height}")
        root.minsize(1060, 700)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        icon = Path(__file__).with_name("app.ico")
        if icon.exists():
            root.iconbitmap(str(icon))
        self._style()
        self._build()
        self._tick()
        if autoload:
            root.after(200, self.refresh_courses)

    def _style(self):
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground=INK,
                        background=BG, borderwidth=0)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Card.TLabel", background="white")
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 22, "bold"), background="white")
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Large.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("TButton", padding=(13, 9), background="#e7edf4", foreground=INK,
                        borderwidth=0, focusthickness=1, focuscolor=ACCENT)
        style.map("TButton", background=[("active", "#dce5ef"), ("disabled", "#eff2f6")],
                  foreground=[("disabled", "#9aa7b4")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white")
        style.map("Accent.TButton", background=[("disabled", "#91bfc3"), ("active", "#066975")],
                  foreground=[("disabled", "#f2fafa")])
        style.configure("Stop.TButton", background="#fae7e6", foreground="#a43734")
        style.map("Stop.TButton", background=[("active", "#f8d7d5"), ("disabled", "#f1eeee")])
        style.configure("TEntry", padding=8, fieldbackground="white", borderwidth=1)
        style.configure("TCombobox", padding=6, fieldbackground="white")
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=34,
                        borderwidth=0, font=("Microsoft YaHei UI", 10))
        style.map("Treeview", background=[("selected", "#d9eff0")], foreground=[("selected", "#075e68")])
        style.configure("Treeview.Heading", background="#e7edf4", foreground=INK,
                        padding=(8, 10), font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 12), background="#e6ecf3")
        style.map("TNotebook.Tab", background=[("selected", "white")],
                  foreground=[("selected", ACCENT)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e0e8f0",
                        borderwidth=0, thickness=12)

    def _load_settings(self):
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return data
        except (OSError, TypeError):
            return {}
        except (ValueError, UnicodeError):
            try:
                stamp = time.strftime('%Y%m%d-%H%M%S')
                backup = self.settings_path.with_name(
                    f"{self.settings_path.stem}.corrupt-{stamp}{self.settings_path.suffix}")
                if backup.exists():
                    backup = self.settings_path.with_name(
                        f"{self.settings_path.stem}.corrupt-{stamp}-{os.getpid()}{self.settings_path.suffix}")
                self.settings_path.replace(backup)
                LOG.warning("桌面设置文件损坏，已备份到 %s", backup)
            except OSError:
                LOG.exception("备份损坏的桌面设置失败")
            return {}

    def _save_settings(self):
        data = {
            "auto_refresh": bool(self.auto_refresh_var.get()),
            "refresh_interval": self._refresh_seconds(),
            "notifications": bool(self.notification_var.get()),
        }
        try:
            data["geometry"] = self.root.geometry()
            data["search"] = self.search.get().strip()
            course = self.selected_course()
            if course:
                data["course_key"] = ":".join(course_key(course))
        except (AttributeError, tk.TclError):
            pass
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.settings_path.with_name(f"{self.settings_path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.settings_path)
        except OSError:
            LOG.exception("保存桌面设置失败")
            try:
                temp.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass

    def _refresh_seconds(self):
        try:
            return {"1 分钟": 60, "5 分钟": 300, "10 分钟": 600, "30 分钟": 1800}[self.auto_refresh_interval_var.get()]
        except (KeyError, TypeError, ValueError):
            return 60

    def button(self, parent, text, command, style="TButton", course=False, account=False):
        button = ttk.Button(parent, text=text, command=command, style=style)
        self.buttons.append((button, course, account))
        return button

    def _build(self):
        header = ttk.Frame(self.root, style="Card.TFrame", padding=(24, 16))
        header.pack(fill="x")
        brand = ttk.Frame(header, style="Card.TFrame")
        brand.pack(side="left")
        ttk.Label(brand, text="学习通助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(brand, text="课程与学习任务，一处管理", style="Card.TLabel",
                  foreground=MUTED).pack(anchor="w", pady=(2, 0))
        tools = ttk.Frame(header, style="Card.TFrame")
        tools.pack(side="right")
        self.account_var = tk.StringVar(value="正在检查本机会话")
        ttk.Label(tools, textvariable=self.account_var, style="Card.TLabel",
                  foreground=ACCENT).grid(row=0, column=0, columnspan=4, sticky="e", pady=(0, 10))
        self.button(tools, "扫码登录", self.qr_login, "Accent.TButton", account=True).grid(row=1, column=0, padx=3)
        self.button(tools, "密码登录", self.password_login, account=True).grid(row=1, column=1, padx=3)
        self.button(tools, "退出账号", self.logout, account=True).grid(row=1, column=2, padx=3)
        ttk.Button(tools, text="使用帮助", command=self.help).grid(row=1, column=3, padx=(3, 0))

        body = ttk.Frame(self.root, padding=(18, 16, 18, 8))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=340)
        left.pack(side="left", fill="y", padx=(0, 18))
        left.pack_propagate(False)
        course_head = ttk.Frame(left)
        course_head.pack(fill="x", pady=(0, 12))
        ttk.Label(course_head, text="我的课程", style="Section.TLabel").pack(side="left")
        self.button(course_head, "刷新列表", self.refresh_courses).pack(side="right")
        refresh_row = ttk.Frame(left)
        refresh_row.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(refresh_row, text="登录后自动刷新课程", variable=self.auto_refresh_var,
                        command=self._auto_refresh_setting_changed).pack(side="left")
        ttk.Label(refresh_row, text="间隔").pack(side="left", padx=(8, 4))
        self.refresh_interval_combo = ttk.Combobox(
            refresh_row, textvariable=self.auto_refresh_interval_var,
            values=("1 分钟", "5 分钟", "10 分钟", "30 分钟"), width=7, state="readonly")
        self.refresh_interval_combo.pack(side="left")
        self.refresh_interval_combo.bind("<<ComboboxSelected>>", self._refresh_interval_changed)
        ttk.Checkbutton(refresh_row, text="Windows 通知", variable=self.notification_var,
                        command=self._notification_setting_changed).pack(side="left", padx=(8, 0))
        self.sync_var = tk.StringVar(value="尚未同步")
        ttk.Label(left, textvariable=self.sync_var, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.search = tk.StringVar(value=str(self.settings.get("search", "")))
        ttk.Entry(left, textvariable=self.search).pack(fill="x")
        ttk.Label(left, text="按课程名或教师搜索", style="Muted.TLabel").pack(anchor="w", pady=(4, 10))
        self.search.trace_add("write", lambda *_: self.filter_courses())
        self.course_table = Table(left, [("name", "课程名称", 230), ("teacher", "教师", 90)])
        self.course_table.pack(fill="both", expand=True)
        self.course_table.tree.bind("<<TreeviewSelect>>", self.on_course_selected)
        self.count_var = tk.StringVar(value="登录后自动读取课程")
        ttk.Label(left, textvariable=self.count_var, style="Muted.TLabel").pack(anchor="w", pady=(9, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        selected = ttk.Frame(right)
        selected.pack(fill="x", pady=(0, 12))
        ttk.Label(selected, text="当前选择", style="Muted.TLabel").pack(anchor="w")
        self.course_var = tk.StringVar(value="请从左侧选择一门课程")
        ttk.Label(selected, textvariable=self.course_var, style="Section.TLabel",
                  wraplength=700).pack(anchor="w", pady=(3, 0))
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)
        pages = []
        for title in ("学习进度", "章节任务", "作业与测验", "课件资料", "通知"):
            page = ttk.Frame(self.notebook, padding=16)
            self.notebook.add(page, text=title)
            pages.append(page)
        self._learning(pages[0])
        self._chapters(pages[1])
        self._works(pages[2])
        self._materials(pages[3])
        self._notices(pages[4])
        footer = ttk.Frame(self.root, padding=(20, 7, 20, 10))
        footer.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(footer, text=f"基于 Superstar Checker · 本地桌面版 {APP_VERSION}",
                  style="Muted.TLabel").pack(side="right")

    def _learning(self, page):
        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=(0, 16))
        self.start_button = self.button(controls, "开始 / 继续学习", self.start_study,
                                        "Accent.TButton", course=True)
        self.start_button.pack(side="left", padx=(0, 10))
        self.resume_button = ttk.Button(controls, text="继续上次任务", command=self.resume_last_study)
        self.resume_button.pack(side="left", padx=(0, 10))
        self.stop_button = ttk.Button(controls, text="停止学习", style="Stop.TButton", command=self.stop_study)
        self.stop_button.pack(side="left")
        ttk.Button(controls, text="导出记录", command=self.export_log).pack(side="left", padx=(10, 0))
        ttk.Label(controls, text="进度自动刷新", style="Muted.TLabel").pack(side="right")
        self.task_var = tk.StringVar(value="尚未启动学习任务")
        self.task_course_var = tk.StringVar(value="选择课程后点击「开始 / 继续学习」")
        ttk.Label(page, textvariable=self.task_var, style="Large.TLabel", wraplength=720).pack(anchor="w")
        ttk.Label(page, textvariable=self.task_course_var, wraplength=720,
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 16))
        self.resume_hint = tk.StringVar(value="")
        ttk.Label(page, textvariable=self.resume_hint, style="Muted.TLabel", wraplength=720).pack(anchor="w")
        self.video_var = tk.StringVar(value="等待开始")
        ttk.Label(page, textvariable=self.video_var, wraplength=720).pack(anchor="w")
        self.progress = ttk.Progressbar(page, maximum=100)
        self.progress.pack(fill="x", pady=(10, 8))
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(page, textvariable=self.time_var, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(page, text="视频按真实时长推进。学习时可最小化窗口，电脑需保持联网。",
                  style="Muted.TLabel", wraplength=720).pack(anchor="w", pady=(12, 16))
        ttk.Label(page, text="最近处理", style="Section.TLabel").pack(anchor="w", pady=(0, 7))
        self.recent = Table(page, [("chapter", "章节", 300), ("result", "结果", 320)], height=2)
        self.recent.pack(fill="both", expand=True)
        ttk.Label(page, text="运行记录", style="Muted.TLabel").pack(anchor="w", pady=(10, 5))
        self.log_text = ScrolledText(page, height=3, wrap="word", state="disabled", relief="flat",
                                     background="white", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        self.log_text.pack(fill="x")

    def _chapters(self, page):
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 12))
        self.button(row, "读取章节", self.load_chapters, course=True).pack(side="left", padx=(0, 8))
        self.button(row, "处理选中章节的文档任务", self.read_chapter, course=True).pack(side="left")
        self.chapter_hint = tk.StringVar(value="读取章节后查看任务数量与完成状态")
        ttk.Label(page, textvariable=self.chapter_hint, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        self.chapter_table = Table(page, [("title", "章节 / 小节", 430),
                                          ("count", "任务数", 75), ("state", "状态", 90)])
        self.chapter_table.pack(fill="both", expand=True)

    def _works(self, page):
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 10))
        self.button(row, "读取作业", self.load_homeworks, course=True).pack(side="left", padx=(0, 7))
        self.button(row, "继续扫描测验", self.load_quizzes, course=True).pack(side="left", padx=(0, 7))
        self.button(row, "查看题目 / 填写答案", self.load_questions, course=True).pack(side="left")
        row2 = ttk.Frame(page)
        row2.pack(fill="x", pady=(0, 10))
        self.button(row2, "查看批阅与得分", self.load_score, course=True).pack(side="left")
        self.work_hint = tk.StringVar(value="测验每次扫描 5 章；可反复点击继续扫描。答案填写后仅暂存。")
        ttk.Label(page, textvariable=self.work_hint, style="Muted.TLabel", wraplength=720).pack(anchor="w", pady=(0, 10))
        self.work_table = Table(page, [("kind", "类型", 65), ("title", "名称", 360),
                                       ("status", "状态", 90), ("time", "截止 / 所在章节", 180)])
        self.work_table.pack(fill="both", expand=True)

    def _materials(self, page):
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="检查前").pack(side="left")
        self.material_limit = tk.StringVar(value="10")
        ttk.Combobox(row, values=(5, 10, 30, 50, 100, 200), textvariable=self.material_limit,
                     width=4, state="readonly").pack(side="left", padx=5)
        ttk.Label(row, text="章").pack(side="left", padx=(0, 8))
        self.button(row, "获取课件", self.load_materials, course=True).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="打开链接", command=self.open_material).pack(side="left", padx=(0, 7))
        ttk.Button(row, text="复制链接", command=self.copy_material).pack(side="left")
        self.material_hint = tk.StringVar(value="读取资料可能需要一些时间，界面仍可操作。")
        ttk.Label(page, textvariable=self.material_hint, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        self.material_table = Table(page, [("name", "资料名称", 340), ("kind", "类型", 75),
                                           ("chapter", "所在章节", 200), ("link", "链接", 75)])
        self.material_table.pack(fill="both", expand=True)
        self.material_table.tree.bind("<Double-1>", lambda _: self.open_material())

    def _notices(self, page):
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 10))
        self.folders = {"默认收件箱": ""}
        self.folder_var = tk.StringVar(value="默认收件箱")
        self.folder_combo = ttk.Combobox(row, textvariable=self.folder_var, values=list(self.folders),
                                         state="readonly", width=16)
        self.folder_combo.pack(side="left", padx=(0, 7))
        self.button(row, "刷新文件夹", self.load_folders).pack(side="left", padx=(0, 7))
        self.unread = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="只看未读", variable=self.unread).pack(side="left")
        row2 = ttk.Frame(page)
        row2.pack(fill="x", pady=(0, 12))
        self.notice_keyword = tk.StringVar()
        ttk.Entry(row2, textvariable=self.notice_keyword, width=22).pack(side="left", padx=(0, 7))
        self.button(row2, "查询通知", self.load_notices).pack(side="left", padx=(0, 7))
        ttk.Button(row2, text="查看内容", command=self.show_notice).pack(side="left")
        self.notice_table = Table(page, [("title", "标题 / 内容", 400), ("sender", "发送者", 120),
                                         ("time", "时间", 130), ("read", "已读", 60)])
        self.notice_table.pack(fill="both", expand=True)
        self.notice_table.tree.bind("<Double-1>", lambda _: self.show_notice())

    def selected_course(self):
        return self.course_table.selected()

    def require_course(self):
        course = self.selected_course()
        if course is None:
            messagebox.showinfo("选择课程", "请先从左侧选择课程。", parent=self.root)
        return course

    def _run(self, title, function, callback=None, course=None, cleanup=None, silent=False,
             retries=0):
        if self.busy or self.closing:
            self.status_var.set("正在处理上一项操作，请稍候。")
            return False
        self.busy = True
        key = course_key(course) if course else None
        self.status_var.set(f"{title}…")
        self.update_buttons()

        def worker():
            data, error, cancelled = None, None, False
            for attempt in range(retries + 1):
                try:
                    data = function()
                    error = None
                    break
                except RefreshCancelled:
                    cancelled = True
                    break
                except SessionExpired:
                    error = "登录已过期，请点击「扫码登录」或「密码登录」。"
                    break
                except Exception as exc:
                    error = str(exc) or "操作失败，请稍后重试。"
                    if attempt < retries and is_transient_network_error(exc):
                        LOG.warning("%s失败，将在后台重试（第 %s 次）: %s", title, attempt + 1, error)
                        # 普通操作的有限重试；自动刷新在独立异步会话中控制总时限。
                        time.sleep((1, 3)[min(attempt, 1)])
                        continue
                    LOG.exception("%s失败", title)
                    break
            self.events.put(("done", title, data, error, callback, key, cleanup, silent, cancelled))

        threading.Thread(target=worker, daemon=True, name="desktop-network").start()
        return True

    def _tick(self):
        if self.closing:
            return
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "qr":
                    self.update_qr(*event[1:])
                    continue
                _, title, data, error, callback, key, cleanup, silent, cancelled = event
                self.busy = False
                if title in {"读取课程", "自动刷新课程"}:
                    self.auto_refresh_inflight = False
                    self._schedule_sync()
                if cleanup:
                    try:
                        cleanup()
                    except Exception:
                        LOG.exception("清理 %s 的界面资源失败", title)
                if cancelled:
                    self.status_var.set("自动刷新已取消")
                elif error:
                    self.status_var.set(error)
                    self.log(error)
                    if title == "自动刷新课程":
                        self.auto_refresh_inflight = False
                        self.auto_refresh_failures += 1
                        self.sync_reason = f"自动刷新失败：{error}"
                        if self.auto_refresh_failures >= 2 and not self.auto_refresh_failure_notified:
                            self._notify("自动刷新失败", "课程列表连续刷新失败，请检查网络或登录状态。", "auto-refresh-failed")
                            self.auto_refresh_failure_notified = True
                    if "登录已过期" in error:
                        self.account_var.set("登录已过期 · 请扫码或输入密码")
                        self._notify("登录已过期", "请重新扫码登录或使用密码登录。", "session-expired")
                    if not silent and "扫码已取消" not in error:
                        messagebox.showerror(title, error, parent=self.root)
                elif key is not None and (not self.selected_course() or course_key(self.selected_course()) != key):
                    self.status_var.set("课程已切换，请为当前课程重新读取。")
                else:
                    if title == "自动刷新课程":
                        self.auto_refresh_inflight = False
                        self.auto_refresh_failures = 0
                        self.auto_refresh_failure_notified = False
                    self.status_var.set(f"{title}完成")
                    self.log(f"{title}完成")
                    if callback:
                        try:
                            callback(data)
                        except Exception:
                            LOG.exception("处理 %s 的完成回调失败", title)
                            self.status_var.set(f"{title}完成，但界面刷新失败")
                self.update_buttons()
        except queue.Empty:
            pass
        except Exception:
            LOG.exception("界面更新失败")
            self.status_var.set("显示更新失败，请重新读取。")
        if time.monotonic()-self.last_poll >= 1:
            self.last_poll = time.monotonic()
            try:
                self.update_progress()
            except Exception:
                LOG.exception("进度读取失败")
        self._maybe_auto_refresh()
        self._update_sync_label()
        self.tick_id = self.root.after(100, self._tick)

    def update_buttons(self):
        course = self.selected_course() is not None
        for button, needs_course, account in self.buttons:
            if not button.winfo_exists():
                continue
            disabled = self.busy or (needs_course and not course) or (account and self.running)
            if button is self.start_button:
                disabled = disabled or self.running
            button.configure(state="disabled" if disabled else "normal")
        self.stop_button.configure(state="normal" if self.running and not self.stop_pending else "disabled",
                                   text="正在停止…" if self.stop_pending else "停止学习")
        if hasattr(self, "resume_button"):
            self.resume_button.configure(state="normal" if self.resume_course and not self.busy and not self.running else "disabled")

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 300:
            self.log_text.delete("1.0", "50.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def export_log(self):
        content = self.log_text.get("1.0", "end-1c").strip()
        if not content:
            messagebox.showinfo("暂无记录", "当前还没有可导出的运行记录。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出运行记录", defaultextension=".txt",
            filetypes=(("文本文件", "*.txt"), ("所有文件", "*.*")),
            initialfile=f"学习通助手运行记录-{time.strftime('%Y%m%d-%H%M%S')}.txt")
        if not path:
            return
        persistent = Path.home() / ".superstar-checker" / "desktop.log"
        try:
            history = persistent.read_text(encoding="utf-8") if persistent.exists() else ""
            course = self.selected_course()
            course_name = course.get("name", "未选择课程") if course else "未选择课程"
            header = (f"学习通助手 {APP_VERSION}\n导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                      f"当前课程：{course_name}\n\n")
            report = header + "[当前会话]\n" + content + "\n"
            if history:
                report += "\n[持久日志]\n" + history
            Path(path).write_text(report, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)
            return
        self.status_var.set(f"运行记录已导出：{Path(path).name}")

    def _refresh_interval_changed(self, _event=None):
        self.auto_refresh_interval = self._refresh_seconds()
        self._schedule_sync()
        self._save_settings()
        self.status_var.set(f"自动刷新间隔已设为 {self.auto_refresh_interval // 60} 分钟")

    def _auto_refresh_setting_changed(self):
        self._schedule_sync()
        if not self.auto_refresh_var.get():
            self.refresh_cancel.set()
        self._save_settings()
        if self.auto_refresh_var.get():
            self.status_var.set(f"已开启自动刷新课程（每 {self.auto_refresh_interval // 60} 分钟）")
        else:
            self.status_var.set("已关闭自动刷新课程")

    def _notification_setting_changed(self):
        self._save_settings()
        self.status_var.set("已开启 Windows 通知" if self.notification_var.get() else "已关闭 Windows 通知")

    def _schedule_sync(self):
        self.next_sync_at = time.monotonic() + self.auto_refresh_interval

    def _notify(self, title, message, key):
        if not self.notification_var.get() or key in self.notify_seen:
            return
        self.notify_seen.add(key)
        try:
            import win32con
            import win32gui
            hwnd = self.root.winfo_id()
            icon_path = Path(__file__).with_name("app.ico")
            hicon = self.notify_handle[1] if self.notify_handle else win32gui.LoadImage(
                None, str(icon_path), win32con.IMAGE_ICON, 0, 0, win32con.LR_LOADFROMFILE)
            nif_icon = getattr(win32gui, "NIF_ICON", getattr(win32con, "NIF_ICON", 0x2))
            nif_tip = getattr(win32gui, "NIF_TIP", getattr(win32con, "NIF_TIP", 0x4))
            nif_info = getattr(win32gui, "NIF_INFO", getattr(win32con, "NIF_INFO", 0x10))
            nim_add = getattr(win32gui, "NIM_ADD", getattr(win32con, "NIM_ADD", 0x0))
            nim_modify = getattr(win32gui, "NIM_MODIFY", getattr(win32con, "NIM_MODIFY", 0x1))
            nid = (hwnd, 0, nif_icon | nif_tip,
                   win32con.WM_USER + 20, hicon, "学习通助手")
            if not self.notify_handle:
                win32gui.Shell_NotifyIcon(nim_add, nid)
                self.notify_handle = (hwnd, hicon)
            info = (hwnd, 0, nif_info, win32con.WM_USER + 20,
                    hicon, "学习通助手", message, 8000, title)
            win32gui.Shell_NotifyIcon(nim_modify, info)
        except Exception:
            LOG.exception("发送 Windows 通知失败")
            self.status_var.set(f"{title}：{message}")

    def _update_sync_label(self):
        if self.auto_refresh_inflight:
            self.sync_var.set("正在自动同步课程…")
            return
        if not self.auto_refresh_var.get():
            self.sync_var.set("自动刷新已关闭")
            return
        if self.running:
            self.sync_var.set("自动刷新已跳过：学习任务运行中")
            return
        if self.busy:
            self.sync_var.set("自动刷新已跳过：其他查询处理中")
            return
        if self.account_var.get().startswith(("未登录", "登录已过期")):
            self.sync_var.set("等待登录后同步")
            return
        if self.sync_reason and self.sync_reason.startswith("自动刷新失败"):
            self.sync_var.set(self.sync_reason)
            return
        if self.last_sync_at is None:
            self.sync_var.set("尚未同步")
            return
        remaining = max(0, int((self.next_sync_at or time.monotonic()) - time.monotonic()))
        minutes, seconds = divmod(remaining, 60)
        stamp = time.strftime("%H:%M:%S", time.localtime(self.last_sync_at))
        self.sync_var.set(f"上次同步 {stamp} · 下次同步 {minutes:02d}:{seconds:02d}")

    def _maybe_auto_refresh(self):
        if not self.auto_refresh_var.get() or self.busy or self.closing:
            return
        if self.running:
            if not self.auto_refresh_pause_noticed:
                self.auto_refresh_pause_noticed = True
                self.status_var.set("自动刷新已跳过：学习任务运行中")
                self.log("自动刷新已跳过：学习任务运行中")
            return
        self.auto_refresh_pause_noticed = False
        now = time.monotonic()
        if self.next_sync_at is not None and now < self.next_sync_at:
            return
        try:
            has_session = self.backend.client.has_session()
        except Exception as exc:
            self._schedule_sync()
            LOG.exception("自动刷新检查登录状态失败")
            self.sync_reason = f"自动刷新失败：{str(exc) or '无法检查登录状态'}"
            self.status_var.set(self.sync_reason)
            return
        if not has_session:
            self._schedule_sync()
            self.account_var.set("未登录 · 请扫码或输入密码")
            self.sync_reason = "未登录，等待登录后同步"
            return
        if self.refresh_courses(auto=True):
            self.auto_refresh_inflight = True

    def refresh_courses(self, auto=False):
        try:
            has_session = self.backend.client.has_session()
        except Exception as exc:
            LOG.exception("检查登录状态失败")
            self.status_var.set(f"无法检查登录状态：{exc}")
            return False
        if not has_session:
            self.account_var.set("未登录 · 请扫码或输入密码")
            self.status_var.set("点击右上角「扫码登录」开始使用")
            self.update_buttons()
            return False
        if self.busy or self.closing:
            return False
        if auto:
            self.refresh_cancel = threading.Event()
            cancellation = self.refresh_cancel
            query = lambda: self.backend.refresh_courses(cancellation)
        else:
            query = self.backend.courses
        started = self._run("自动刷新课程" if auto else "读取课程", query,
                            self.show_courses, silent=auto)
        return started

    def show_courses(self, courses):
        self.all_courses = sorted(courses, key=lambda c: not c.get("active", True))
        self.account_var.set(f"已登录 · 账号 {self.backend.client.uid}")
        self.last_sync_at = time.time()
        self._schedule_sync()
        self.auto_refresh_failures = 0
        self.auto_refresh_failure_notified = False
        self.notify_seen.discard("auto-refresh-failed")
        self.sync_reason = ""
        self.filter_courses()

    def filter_courses(self):
        search = self.search.get().strip().lower()
        courses = [c for c in self.all_courses if search in (c.get("name", "")+" "+c.get("teacher", "")).lower()]
        preferred = self.restore_course_key
        self.course_table.fill(courses,
            lambda c: (c.get("name", ""), c.get("teacher", "")),
            key=lambda c: ":".join(course_key(c)), select_first=not preferred)
        if preferred:
            preferred_id = ":".join(preferred) if isinstance(preferred, (list, tuple)) else str(preferred)
            if preferred_id in self.course_table.rows:
                self.course_table.tree.selection_set(preferred_id)
                self.course_table.tree.see(preferred_id)
            self.restore_course_key = None
        self.count_var.set(f"显示 {len(courses)} / {len(self.all_courses)} 门课程")
        self.on_course_selected()

    def on_course_selected(self, _event=None):
        course = self.selected_course()
        key = course_key(course) if course else None
        if key != self.last_course_key:
            self.last_course_key = key
            self.course_var.set(course["name"] if course else "请从左侧选择一门课程")
            self.homework_rows, self.quiz_rows = [], []
            for table in (self.chapter_table, self.work_table, self.material_table):
                table.fill([], lambda _: ())
            self.chapter_hint.set("读取章节后查看任务数量与完成状态")
            self.work_hint.set("测验每次扫描 5 章；可反复点击继续扫描。答案填写后仅暂存。")
            self.material_hint.set("点击获取课件，读取选定范围内的附件。")
            self.close_work_windows()
        self.update_buttons()

    def update_progress(self):
        state = self.backend.status()
        self.running = bool(state.get("running"))
        self._update_resume_candidate(state)
        if not self.running:
            self.stop_pending = False
        current = state.get("current") or "尚未启动学习任务"
        failures = sum(1 for r in state.get("results", []) for item in r.get("results", [])
                       if item.get("status") == "failed")
        if current == "全部完成" and failures:
            current = f"本轮已结束 · {failures} 个任务失败，可检查后重试"
        self.task_var.set("正在学习" if self.running else current)
        running_course = next((c.get("name") for c in self.all_courses
                               if str(c.get("course_id")) == str(state.get("course_id"))
                               and str(c.get("class_id")) == str(state.get("class_id"))), "")
        context = running_course or ("选择课程后点击「开始 / 继续学习」" if not state.get("course_id") else "已有学习记录")
        if self.running:
            context += f" · 已处理 {len(state.get('results', []))} / {state.get('pending_total', '—')} 章"
        self.task_course_var.set(context)
        video = state.get("video_progress") or {}
        playing, total = video.get("playing", 0), video.get("duration", 0)
        self.video_var.set(video.get("name") or state.get("current") or "等待开始")
        self.progress["value"] = min(100, playing/total*100) if total else 0
        self.time_var.set(f"{duration(playing)} / {duration(total)}" + (f" · 第 {state['chapter_index']} 章" if state.get("chapter_index") else ""))
        recent = state.get("results", [])[-5:]
        self.recent.fill(recent, lambda r: (r.get("chapter", ""), r.get("summary", "")))
        signature = (self.running, state.get("current"), len(state.get("results", [])))
        if signature != self.last_signature and state.get("current"):
            self.log(f"任务：{current}")
            if state.get("error"):
                self.log(f"异常：{state['error']}")
        self.last_signature = signature
        run_key = ":".join(str(state.get(name, "")) for name in
                           ("uid", "course_id", "class_id", "cpi", "started_at"))
        result_key = f"{run_key}:{state.get('updated_at', '')}:{len(state.get('results', []))}"
        if self.last_running_state and not self.running:
            if state.get("current") == "已停止":
                self._notify("学习任务已停止", "当前进度已保留。", f"study-stopped:{result_key}")
            elif failures or state.get("error") or state.get("current") in {"异常停止", "任务进程已退出，可重新开始"}:
                self._notify("学习任务异常结束", state.get("error") or "有章节处理失败，可检查后继续。",
                             f"study-failed:{result_key}")
            elif state.get("current") == "全部完成":
                self._notify("学习任务已完成", state.get("current"), f"study-complete:{result_key}")
        self.last_running_state = self.running
        if self.running != self.awake and os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if self.running else 0x80000000)
            self.awake = self.running
        self.update_buttons()

    def _update_resume_candidate(self, state):
        if state.get("running") or not state.get("resumable"):
            self.resume_course = None
            self.resume_hint.set("")
            self.update_buttons()
            return
        target = next((course for course in self.all_courses
                       if str(course.get("course_id")) == str(state.get("course_id"))
                       and str(course.get("class_id")) == str(state.get("class_id"))
                       and str(course.get("cpi")) == str(state.get("cpi"))), None)
        self.resume_course = target
        if target:
            self.resume_hint.set(f"检测到上次任务未完成：{target.get('name', '当前课程')} · 点击「继续上次任务」恢复")
        else:
            self.resume_hint.set("检测到未完成任务，但课程列表中暂时找不到对应课程")
        self.update_buttons()

    def resume_last_study(self):
        if self.busy or self.closing:
            return
        state = self.backend.status()
        if state.get("running"):
            self.update_progress()
            self.status_var.set("已有学习任务运行，请先停止当前任务。")
            return
        self._update_resume_candidate(state)
        course = self.resume_course
        if not course:
            self.status_var.set("上次任务已结束或课程已不存在，请刷新课程列表。")
            return
        self._clear_study_notifications()
        self.search.set("")
        self.filter_courses()
        self.course_table.tree.selection_set(":".join(course_key(course)))
        self.on_course_selected()
        self.notebook.select(0)
        self._run(f"继续《{course['name']}》", lambda: self.backend.start(course),
                  lambda _: self.update_progress(), course=course)

    def _clear_study_notifications(self):
        self.notify_seen = {key for key in self.notify_seen if not key.startswith("study-")}

    def start_study(self):
        course = self.require_course()
        if course:
            self._clear_study_notifications()
            self.notebook.select(0)
            self._run(f"启动《{course['name']}》", lambda: self.backend.start(course),
                      lambda _: self.update_progress())

    def stop_study(self):
        try:
            self.backend.stop()
            self.stop_pending = True
            self.status_var.set("已请求停止，正在保存当前进度。")
            self.log("已请求停止学习")
            self.update_buttons()
        except Exception as exc:
            messagebox.showerror("停止失败", str(exc), parent=self.root)

    def load_chapters(self):
        course = self.require_course()
        if course:
            self._run("读取章节", lambda: self.backend.chapters(course), self.show_chapters, course=course)

    def show_chapters(self, chapters):
        self.chapter_table.fill(chapters, lambda c: (c["title"], c["job_count"],
                                  "已完成" if c["finished"] else "待处理"))
        done = sum(bool(c["finished"]) for c in chapters)
        self.chapter_hint.set(f"共 {len(chapters)} 个小节 · {done} 个已完成 · 任务数为平台提供的任务点数量")

    def read_chapter(self):
        course, chapter = self.require_course(), self.chapter_table.selected()
        if not course:
            return
        if not chapter:
            messagebox.showinfo("选择章节", "请先读取并选中一个章节。", parent=self.root)
            return
        if self.running:
            messagebox.showinfo("任务运行中", "请先停止当前学习任务，再单独处理文档。", parent=self.root)
            return
        self._run("处理文档任务", lambda: self.backend.read_chapter(course, chapter),
                  lambda rows: self.text_dialog("文档任务结果", "\n".join(
                      f"{r.get('name', '')}：{r.get('detail', r.get('status', ''))}" for r in rows) or "该章节未发现文档或阅读任务。"),
                  course=course)

    def load_homeworks(self):
        course = self.require_course()
        if course:
            def show(rows):
                self.homework_rows = [dict(r, kind="homework") for r in rows]
                self.show_works()
                self.work_hint.set(f"读取到 {len(rows)} 份作业。选择后可查看题目或已批阅成绩。")
            self._run("读取作业", lambda: self.backend.homeworks(course), show, course=course)

    def load_quizzes(self):
        course = self.require_course()
        if course:
            def show(result):
                self.quiz_rows = [dict(r, kind="quiz") for r in result["items"]]
                self.show_works()
                self.work_hint.set(f"已扫描 {result['scanned']} / {result['total']} 章 · 发现 {len(self.quiz_rows)} 份测验。可继续扫描。")
            self._run("扫描章节测验", lambda: self.backend.quizzes(course), show, course=course)

    def show_works(self):
        self.work_table.fill(self.homework_rows+self.quiz_rows,
            lambda r: ("测验" if r["kind"] == "quiz" else "作业",
                       r.get("title") or r.get("name", ""),
                       ("已完成" if r.get("isPassed") else "待处理") if r["kind"] == "quiz" else r.get("status", ""),
                       r.get("deadline") or r.get("chapter", "")))

    def require_work(self):
        row = self.work_table.selected()
        if row is None:
            messagebox.showinfo("选择作业", "请先读取作业或扫描测验，再选中一行。", parent=self.root)
        return row

    def load_questions(self):
        course, row = self.require_course(), self.require_work()
        if course and row:
            self._run("读取题目", lambda: self.backend.questions(course, row),
                      lambda data: self.question_dialog(row.get("title") or row.get("name", "题目"), data),
                      course=course)

    def load_score(self):
        course, row = self.require_course(), self.require_work()
        if course and row:
            def show(result):
                lines = [f"总分：{result.get('total_score') or '平台未提供'}"]
                if result.get("attempt_scores"):
                    lines.append("历次得分："+" / ".join(result["attempt_scores"]))
                for index, q in enumerate(result.get("questions", []), 1):
                    lines.extend([f"\n{index}. {q.get('title', '')}",
                                  f"我的答案：{q.get('my_answer', '未提供')}",
                                  f"正确答案：{q.get('correct_answer', '未提供')}"])
                if result.get("raw_excerpt"):
                    lines.append(result["raw_excerpt"])
                self.text_dialog("批阅与得分", "\n".join(lines))
            self._run("读取批阅", lambda: self.backend.score(course, row), show, course=course)

    def load_materials(self):
        course = self.require_course()
        if course:
            limit = int(self.material_limit.get())
            def show(rows):
                names = {"document": "文档", "video": "视频", "media": "媒体", "audio": "音频", "other": "附件"}
                self.material_table.fill(rows, lambda r: (r["name"], names.get(r["kind"], r["kind"]),
                                                          r["chapter"], "可打开" if r.get("url") else "无链接"))
                self.material_hint.set(f"检查前 {limit} 章，读取到 {len(rows)} 份附件。可增大章节范围继续查询。")
            self._run("获取课件", lambda: self.backend.materials(course, limit), show, course=course)

    def material_url(self):
        row = self.material_table.selected()
        if not row:
            messagebox.showinfo("选择课件", "请先获取课件并选中一行。", parent=self.root)
            return None
        try:
            return safe_url(row.get("url"))
        except ValueError as exc:
            messagebox.showinfo("无法打开", str(exc), parent=self.root)

    def open_material(self):
        url = self.material_url()
        if url:
            webbrowser.open(url)

    def copy_material(self):
        url = self.material_url()
        if url:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.status_var.set("课件链接已复制")

    def load_folders(self):
        def show(rows):
            self.folders = {"默认收件箱": ""}
            for row in rows:
                self.folders[f"{row['name']}（未读 {row['unread']}）"] = row["uuid"]
            self.folder_combo.configure(values=list(self.folders))
            self.folder_var.set("默认收件箱")
        self._run("读取通知文件夹", self.backend.folders, show)

    def load_notices(self):
        folder = self.folders.get(self.folder_var.get(), "")
        keyword, unread = self.notice_keyword.get().strip(), self.unread.get()
        def show(rows):
            self.notice_table.fill(rows, lambda r: (plain_text(r.get("title") or r.get("content", "")),
                                                    r.get("sender", ""), r.get("time", ""),
                                                    "是" if r.get("is_read") else "否"))
        self._run("查询通知", lambda: self.backend.notices(folder, keyword, unread), show)

    def show_notice(self):
        row = self.notice_table.selected()
        if not row:
            messagebox.showinfo("选择通知", "请先查询通知并选中一行。", parent=self.root)
            return
        text = "\n\n".join(filter(None, [row.get("title"), f"发送者：{row.get('sender') or '—'}",
                          f"时间：{row.get('time') or '—'}", plain_text(row.get("content")), row.get("publish_time")]))
        self.text_dialog("通知内容", text, url=row.get("url"))

    def question_dialog(self, title, data):
        work_id, questions = data
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("920x730")
        win.minsize(720, 550)
        self.work_windows.append(win)
        ttk.Label(win, text=title, style="Section.TLabel", wraplength=850).pack(anchor="w", padx=20, pady=(16, 5))
        ttk.Label(win, text="填写后点击暂存。正式提交请在学习通内核对后操作。", style="Muted.TLabel").pack(anchor="w", padx=20, pady=(0, 12))
        holder = ttk.Frame(win)
        holder.pack(fill="both", expand=True, padx=16)
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        form = ttk.Frame(canvas, padding=(4, 0, 16, 10))
        item = canvas.create_window(0, 0, window=form, anchor="nw")
        form.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        inputs = {}
        for index, question in enumerate(questions, 1):
            box = ttk.Frame(form, padding=(0, 12))
            box.pack(fill="x")
            ttk.Label(box, text=f"{index}. [{question.get('type_name', '')}] {question['title']}",
                      wraplength=790).pack(anchor="w")
            for option in question.get("options", []):
                ttk.Label(box, text=option, wraplength=770, foreground=MUTED).pack(anchor="w", padx=10, pady=2)
            qtype = str(question.get("type"))
            if qtype == "3":
                variable = tk.StringVar()
                widget = ttk.Combobox(box, textvariable=variable, values=("true", "false"), state="readonly", width=15)
                inputs[question["id"]] = variable.get
            elif qtype in ("2", "6", "7", "8"):
                widget = tk.Text(box, height=3, wrap="word", relief="flat", font=("Microsoft YaHei UI", 10))
                inputs[question["id"]] = lambda w=widget: w.get("1.0", "end-1c").strip()
            else:
                variable = tk.StringVar()
                widget = ttk.Entry(box, textvariable=variable)
                inputs[question["id"]] = variable.get
                hint = "多个空用 | 分隔" if qtype == "4" else "单选填 A；多选填 ABD"
                ttk.Label(box, text=hint, style="Muted.TLabel").pack(anchor="w", pady=(4, 2))
            widget.pack(fill="x", pady=(6, 0))
        bottom = ttk.Frame(win, padding=16)
        bottom.pack(fill="x")

        def save():
            answers = {key: getter().strip() for key, getter in inputs.items()}
            missing = sum(not answer for answer in answers.values())
            if missing:
                messagebox.showinfo("尚未填完", f"还有 {missing} 道题未填写，请完成后再暂存。", parent=win)
                return
            self._run("暂存答案", lambda: self.backend.save_answers(work_id, answers),
                      lambda _: messagebox.showinfo("暂存成功", "答案已暂存。请在学习通中核对并手动提交。", parent=win))

        self.button(bottom, "暂存答案", save, "Accent.TButton").pack(side="right")
        ttk.Button(bottom, text="关闭", command=win.destroy).pack(side="right", padx=8)

    def text_dialog(self, title, text, url=None):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("820x580")
        self.work_windows.append(win)
        content = ScrolledText(win, wrap="word", relief="flat", padx=16, pady=16,
                               font=("Microsoft YaHei UI", 10), background="white", foreground=INK)
        content.pack(fill="both", expand=True, padx=16, pady=16)
        content.insert("1.0", text)
        content.configure(state="disabled")
        if url:
            try:
                url = safe_url(url)
                ttk.Button(win, text="在浏览器打开", command=lambda: webbrowser.open(url)).pack(pady=(0, 12))
            except ValueError:
                pass

    def close_work_windows(self):
        for win in self.work_windows:
            if win.winfo_exists():
                win.destroy()
        self.work_windows.clear()
        self.buttons = [(b, c, a) for b, c, a in self.buttons if b.winfo_exists()]

    def qr_login(self):
        if self.busy or self.running:
            return
        win = tk.Toplevel(self.root)
        win.title("学习通扫码登录")
        win.geometry("460x510")
        win.resizable(False, False)
        win.transient(self.root)
        self.qr_window = win
        self.qr_cancel = threading.Event()
        self.qr_title = tk.StringVar(value="正在生成二维码…")
        self.qr_hint = tk.StringVar(value="请稍候")
        ttk.Label(win, textvariable=self.qr_title, style="Section.TLabel").pack(pady=(24, 16))
        blank = tk.PhotoImage(width=1, height=1)
        self.qr_image = tk.Label(win, bg="white", image=blank, width=300, height=300)
        self.qr_image.photo = blank
        self.qr_image.pack()
        ttk.Label(win, textvariable=self.qr_hint, style="Muted.TLabel").pack(pady=12)

        def cancel():
            self.qr_cancel.set()
            self.qr_title.set("正在取消…")
        ttk.Button(win, text="取消扫码", command=cancel).pack()
        win.protocol("WM_DELETE_WINDOW", cancel)
        cancellation = self.qr_cancel
        self._run("扫码登录", lambda: self.backend.login_qr(
            lambda *args: self.events.put(("qr", *args)), cancellation),
            self.logged_in, cleanup=self.close_qr)

    def update_qr(self, png, text, hint):
        if self.qr_window and self.qr_window.winfo_exists():
            self.qr_title.set(text)
            self.qr_hint.set(hint)
            if png:
                photo = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
                self.qr_image.configure(image=photo, width=300, height=300)
                self.qr_image.photo = photo

    def close_qr(self):
        if self.qr_window and self.qr_window.winfo_exists():
            self.qr_window.destroy()
        self.qr_window, self.qr_cancel = None, None

    def password_login(self):
        win = tk.Toplevel(self.root)
        win.title("密码登录")
        win.geometry("440x350")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        form = ttk.Frame(win, padding=22)
        form.pack(fill="both", expand=True)
        ttk.Label(form, text="手机号").pack(anchor="w")
        phone = ttk.Entry(form)
        phone.pack(fill="x", pady=(5, 12))
        ttk.Label(form, text="密码").pack(anchor="w")
        password = ttk.Entry(form, show="●")
        password.pack(fill="x", pady=(5, 10))
        ttk.Label(form, text="密码仅用于本次登录，不保存在程序中。", style="Muted.TLabel").pack(anchor="w")

        def login(_event=None):
            number, secret = phone.get().strip(), password.get()
            if not number or not secret:
                messagebox.showinfo("请填写", "请输入手机号和密码。", parent=win)
                return
            password.delete(0, "end")
            win.destroy()
            self._run("密码登录", lambda: self.backend.password_login(number, secret), self.logged_in)
        ttk.Button(form, text="登录", command=login, style="Accent.TButton").pack(anchor="e", pady=(12, 0))
        win.bind("<Return>", login)
        phone.focus_set()

    def logged_in(self, _result=None):
        self.notify_seen.discard("session-expired")
        self.close_work_windows()
        self.all_courses = []
        self.search.set("")
        self.notice_table.fill([], lambda _: ())
        self.folders = {"默认收件箱": ""}
        self.folder_combo.configure(values=list(self.folders))
        self.folder_var.set("默认收件箱")
        self.refresh_courses()

    def logout(self):
        if not messagebox.askyesno("退出账号", "确定清除本机的学习通登录会话？", parent=self.root):
            return
        def done(_):
            self.close_work_windows()
            self.all_courses = []
            self.filter_courses()
            self.notice_table.fill([], lambda _: ())
            self.account_var.set("未登录 · 请扫码或输入密码")
        self._run("退出账号", self.backend.logout, done)

    def help(self):
        self.text_dialog("使用帮助", f"学习通助手 · 本地桌面版 {APP_VERSION}\n\n"
            "1. 首次使用点击扫码登录，用学习通 App 扫描并在手机确认。也可使用密码登录。\n"
            "2. 从左侧选择课程，点击开始 / 继续学习。进度自动更新，可随时停止。\n"
            "3. 左侧默认开启自动刷新课程，可选择 1、5、10 或 30 分钟间隔；界面会显示最近同步时间、倒计时和跳过原因，网络失败只会有限重试。\n"
            "4. Windows 通知默认开启，学习完成、失败、登录过期或连续刷新失败时提醒一次，可在左侧关闭。\n"
            "5. 如果上次任务异常停止或手动停止且仍有未完成内容，学习进度页会显示继续上次任务；需要点击按钮确认，程序不会自动刷课。\n"
            "6. 学习期间保持程序运行。可最小化窗口，程序会请求系统保持唤醒；手动睡眠、关机或断网仍会中断。\n"
            "7. 章节、作业、课件和通知在对应标签页中读取。部分查询需要等待，停止按钮仍可使用。\n"
            "8. 题目支持手动填写与暂存，正式提交需在学习通中核对后完成。\n\n"
            "9. 学习进度页可导出当前会话记录和完整 desktop.log，便于排查网络或任务失败。\n\n"
            "本桌面版基于 MicroYume/superstar-checker（MIT），非学习通官方客户端。\n"
            "登录会话仅保存在本机 .superstar-checker 目录，请勿分享 session.json。\n"
            "平台接口变化可能影响查询和学习功能。")

    def callback_error(self, kind, value, traceback):
        LOG.error("界面错误", exc_info=(kind, value, traceback))
        messagebox.showerror("操作未完成", str(value), parent=self.root)

    def _retry_or_force_close(self, title, detail):
        retry = messagebox.askyesno(
            title, f"{detail}\n\n点击“是”重试，点击“否”强制退出。", parent=self.root)
        if retry:
            self.root.after(0, self.close)
        else:
            self.destroy()

    def close(self):
        try:
            state = self.backend.status()
        except Exception as exc:
            LOG.exception("关闭时读取学习状态失败")
            self.log(f"关闭时读取学习状态失败：{exc}")
            self._retry_or_force_close("无法确认任务状态", "无法确认学习任务是否仍在运行。")
            return
        if state.get("running"):
            if not messagebox.askyesno("停止并退出", "学习任务正在运行。确定停止任务并退出程序？", parent=self.root):
                return
            try:
                self.backend.stop()
            except Exception as exc:
                LOG.exception("关闭时请求停止学习失败")
                self._retry_or_force_close("停止失败", f"无法发送停止请求：{exc}")
                return
            self.stop_pending = True
            self.status_var.set("正在等待学习任务停止，请稍候…")
            self._wait_to_close(time.monotonic()+35)
            return
        self.destroy()

    def _wait_to_close(self, deadline):
        try:
            running = self.backend.status().get("running")
        except Exception as exc:
            LOG.exception("等待关闭时读取学习状态失败")
            self._retry_or_force_close("无法确认任务状态", f"等待任务停止时读取状态失败：{exc}")
            return
        if not running:
            self.destroy()
        elif time.monotonic() > deadline:
            self._retry_or_force_close("任务仍在停止", "停止请求已发送，但任务尚未确认结束。")
        else:
            self.root.after(300, lambda: self._wait_to_close(deadline))

    def destroy(self):
        self._save_settings()
        self.closing = True
        self.refresh_cancel.set()
        if self.tick_id:
            self.root.after_cancel(self.tick_id)
        if self.qr_cancel:
            self.qr_cancel.set()
        if self.notify_handle:
            try:
                import win32gui
                delete_code = getattr(win32gui, "NIM_DELETE", 0x2)
                win32gui.Shell_NotifyIcon(delete_code,
                                          (self.notify_handle[0], 0, 0, 0, 0, ""))
                win32gui.DestroyIcon(self.notify_handle[1])
            except Exception:
                LOG.exception("清理 Windows 通知图标失败")
            self.notify_handle = None
        if os.name == "nt" and self.awake:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        self.root.destroy()


def check_install(destination):
    """离线验证资源及依赖；不读取本机账号或访问平台。"""
    from superstar_checker.crypto import encrypt_aes, encrypt_des
    from superstar_checker import secret_font
    report = {"app_version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
              "python": sys.version.split()[0]}
    root = tk.Tk()
    root.withdraw()
    report["tk"] = root.tk.call("info", "patchlevel")
    root.destroy()
    report["aes_ok"] = bool(encrypt_aes("installation-check"))
    report["des_ok"] = bool(encrypt_des("installation-check"))
    try:
        import win32con  # noqa: F401
        import win32gui  # noqa: F401
        report["notifications_ok"] = True
    except Exception as exc:
        report["notifications_ok"] = False
        report["notifications_error"] = str(exc)
    font_file = Path(secret_font.__file__).parent / "data" / "font_map_table.json"
    report["font_table_entries"] = len(json.loads(font_file.read_text(encoding="utf-8")))
    report["offline"] = True
    Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def smoke_test(destination):
    """隔离模拟后端，验证真实主窗口和关闭路径。"""
    import tempfile
    from types import SimpleNamespace

    class SmokeBackend:
        client = SimpleNamespace(uid="isolated-smoke", has_session=lambda: True)
        def status(self):
            return {"running": False}
        def courses(self):
            return [{"course_id": 1, "class_id": 2, "cpi": 3,
                     "name": "隔离测试课程", "teacher": "模拟后端", "active": True}]

    with tempfile.TemporaryDirectory(prefix="superstar-smoke-") as directory:
        root = tk.Tk()
        app = App(root, SmokeBackend(), autoload=False,
                  settings_path=Path(directory) / "settings.json")
        errors = []
        root.report_callback_exception = lambda *exc: errors.append(str(exc[1]))
        app.notification_var.set(False)
        app.auto_refresh_var.set(False)
        app.refresh_courses()
        timeout = time.monotonic() + 5
        while app.busy and time.monotonic() < timeout:
            root.update()
            time.sleep(0.01)
        root.update()
        report = {"app_version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
                  "mock_backend": True, "course_count": len(app.all_courses),
                  "window_visible": bool(root.winfo_viewable()), "errors": errors}
        app.close()
        report["closed"] = app.closing and app.refresh_cancel.is_set()
        Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if app.busy or errors or report["course_count"] != 1 or not report["closed"]:
            raise RuntimeError("隔离窗口检查失败")


def main():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--check-install")
    parser.add_argument("--smoke-test")
    args = parser.parse_args()
    if args.check_install:
        check_install(args.check_install)
        return
    if args.smoke_test:
        smoke_test(args.smoke_test)
        return
    log_dir = Path.home() / ".superstar-checker"
    log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(log_dir / "desktop.log", maxBytes=1_000_000,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    mutex = None
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        api.CreateMutexW.restype = ctypes.c_void_p
        mutex = api.CreateMutexW(None, False, "Local\\SuperstarCheckerDesktopV1")
        if ctypes.get_last_error() == 183:
            ctypes.windll.user32.MessageBoxW(None, "程序已在运行，请从任务栏打开现有窗口。", "学习通助手", 0)
            return
    root = tk.Tk()
    App(root)
    try:
        root.mainloop()
    finally:
        if mutex:
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            api.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("程序启动失败")
        if _diagnostic_directory is not None:
            flag = "--check-install" if "--check-install" in sys.argv else "--smoke-test"
            destination = sys.argv[sys.argv.index(flag) + 1]
            Path(destination).write_text(json.dumps({"app_version": APP_VERSION,
                "error": str(sys.exc_info()[1])}, ensure_ascii=False, indent=2), encoding="utf-8")
        elif os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, "程序未能启动。请查看本机 .superstar-checker/desktop.log。", "学习通助手", 16)
        raise
