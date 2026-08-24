"""Tree Agent — a Windows GUI over the Codex CLI with a real project tree.

What this adds on top of the Codex TUI:
  * projects nest arbitrarily deep (project -> sub-project -> sub-project ...);
  * every conversation belongs to exactly one project and keeps its own Codex
    thread, so switching between them never mixes context;
  * working directory / model / sandbox are set per project and inherited by
    sub-projects and conversations;
  * several conversations can run at the same time.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import time
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

if __package__ in (None, ""):  # allow `python app.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tree_agent import clipboard_image, codex_runner, richtext, store, transfer
else:
    from . import clipboard_image, codex_runner, richtext, store, transfer

APP_NAME = "Tree Agent"

UI_FONT = "Microsoft JhengHei UI"
MONO_FONT = "Consolas"

LIGHT_COLORS = {
    "bg": "#f6f7f9",
    "panel": "#ffffff",
    "border": "#d9dce1",
    "text": "#1f2328",
    "muted": "#6b7280",
    "user": "#1a56db",
    "agent": "#111827",
    "reasoning": "#7c8391",
    "tool": "#374151",
    "tool_bg": "#f2f4f7",
    "error": "#b91c1c",
    "notice": "#b45309",
    "accent": "#1a56db",
    "user_bg": "#e8f0fe",
    "warn_bg": "#fff7e6",
    "select": "#bcd4f6",
    "select_idle": "#dde5f0",
    "tree_conversation": "#334155",
    "drop_target": "#e6f0ff",
    "log": "#9aa1ab",
    "tooltip": "#333a44",
}

DARK_COLORS = {
    "bg": "#1b1f27",
    "panel": "#252b36",
    "border": "#3a4352",
    "text": "#e6edf3",
    "muted": "#a8b3c3",
    "user": "#8ab4ff",
    "agent": "#e6edf3",
    "reasoning": "#b5becd",
    "tool": "#ced8e6",
    "tool_bg": "#202631",
    "error": "#ff8c8c",
    "notice": "#ffd580",
    "accent": "#8ab4ff",
    "user_bg": "#263b5b",
    "warn_bg": "#423820",
    "select": "#35557f",
    "select_idle": "#2b3d56",
    "tree_conversation": "#c1cede",
    "drop_target": "#304e75",
    "log": "#a6b0bd",
    "tooltip": "#111827",
}

# Kept mutable because richtext receives this palette by reference.  Updating
# it in place lets a live transcript change appearance without rebuilding it.
COLORS = dict(LIGHT_COLORS)

THEME_LIGHT = "light"
THEME_DARK = "dark"

ROLE_LABELS = {
    "user": "你",
    "agent": "Codex",
    "reasoning": "思考",
    "tool": "工具",
    "error": "錯誤",
    "notice": "提示",
    "meta": "系統",
}

INHERIT = "(繼承)"
AGENT_LABELS = {
    store.CODEX_AGENT: "Codex CLI",
    store.CLAUDE_AGENT: "Claude Code",
}

PUMP_INTERVAL_MS = 60          # how often worker-thread events reach the UI
FLUSH_EVERY_TICKS = 8         # coalesce streamed message writes (~0.5s)
THUMBNAIL_HEIGHT = 48         # attachment preview height, in pixels
# Stand-in prompt when you attach images without typing anything.
IMAGE_ONLY_PROMPT = "請看附加的圖片。"

# How much of a command execution to draw. Codex can emit hundreds of lines of
# `git status` or a file listing, which buries the answer it was working towards.
TOOL_COLLAPSED = "collapsed"
TOOL_FULL = "full"
TOOL_HIDDEN = "hidden"
TOOL_DISPLAY_LABELS = (
    (TOOL_COLLAPSED, "收合成一行（可點開）"),
    (TOOL_FULL, "完整顯示"),
    (TOOL_HIDDEN, "完全隱藏"),
)
TOOL_SUMMARY_CHARS = 110      # command text kept on the summary line

# Tk's Text has no maximum-width option, so the container is padded instead.
# Unbounded, a maximised window gives ~237 latin characters per line; comfortable
# reading is well under half that.
MAX_CONTENT_PX = 980
# The gutters left over by the width cap: an outline rail on one side, a details
# panel on the other. Both fold away automatically when the window is too narrow
# to give the transcript a usable measure.
OUTLINE_WIDTH = 250           # starting width; drag the sash to change it
INFO_WIDTH = 290
MIN_CONTENT_PX = 560
MIN_RAIL_PX = 120             # a rail narrower than this is not worth keeping
TEXT_INSET_PX = 14            # the transcript's own breathing room
# A turn routinely runs for tens of seconds, so show what it is doing.
STAGE_LABELS = {
    "start": "啟動中",
    "thinking": "思考中",
    "tool": "執行指令",
    "writing": "回覆中",
}
PROGRESS_TICKS = 8            # refresh the elapsed counter about twice a second

# Transcripts written before attachments were drawn from metadata carry the
# file names inside the message text; this strips those duplicate lines.
_ATTACHMENT_LINE = re.compile(r"^\s*🖼.*$", re.M)


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _pick_font(root: tk.Tk, preferred: str, fallback: str) -> str:
    from tkinter import font as tkfont

    return preferred if preferred in tkfont.families(root) else fallback


class TreeAgentApp:
    def __init__(
        self,
        root: tk.Tk,
        home: str = store.DEFAULT_HOME,
        single_instance: bool = True,
    ) -> None:
        self.root = root
        self.lock = store.WorkspaceLock(home) if single_instance else None
        if self.lock is not None and not self.lock.acquire():
            if not messagebox.askyesno(
                APP_NAME,
                f"這個工作區已被另一個 Tree Agent 視窗開啟（PID {self.lock.holder_pid()}）：\n"
                f"{home}\n\n"
                "同時開啟會讓兩邊互相覆蓋專案與對話。\n"
                "建議改用 --home 指定另一個工作區。\n\n"
                "仍要繼續嗎？",
            ):
                root.destroy()
                raise SystemExit(0)
            self.lock = None  # user chose to proceed unprotected

        self.ws = store.Workspace(home)
        self.turns: dict[str, codex_runner.Turn] = {}
        # Each turn is stamped with a serial number. Cancelling is asynchronous,
        # so events already queued by a killed turn can still arrive after the
        # conversation was reset or deleted — they are dropped by comparing the
        # stamp against the conversation's current turn.
        self.turn_serials: dict[str, int] = {}
        self.turn_started: dict[str, float] = {}
        self.turn_stage: dict[str, str] = {}
        self._turn_counter = 0
        self.events: queue.Queue[tuple[str, int, dict[str, Any]]] = queue.Queue()
        self.current_id: str | None = None
        self.tool_display = (self.ws.data.get("ui") or {}).get(
            "tool_display", TOOL_COLLAPSED
        )
        if self.tool_display not in dict(TOOL_DISPLAY_LABELS):
            self.tool_display = TOOL_COLLAPSED
        ui_state = self.ws.data.get("ui") or {}
        self.theme = ui_state.get("theme", THEME_LIGHT)
        if self.theme not in (THEME_LIGHT, THEME_DARK):
            self.theme = THEME_LIGHT
        self._set_palette()
        self.show_outline = bool(ui_state.get("show_outline", True))
        self.show_info = bool(ui_state.get("show_info", True))
        self.limit_width = bool(ui_state.get("limit_width", True))
        self.outline_width = max(MIN_RAIL_PX,
                                 int(ui_state.get("outline_width") or OUTLINE_WIDTH))
        self.info_width = max(MIN_RAIL_PX,
                              int(ui_state.get("info_width") or INFO_WIDTH))
        self._drag_id: str | None = None
        self._drag_started = False

        self.ui_font = _pick_font(root, UI_FONT, "Segoe UI")
        self.mono_font = _pick_font(root, MONO_FONT, "Courier New")

        self._build_window()
        self._build_styles()
        self._build_menu()
        self._build_layout()
        self.refresh_tree()
        self._restore_selection()
        self._pump_job: str | None = None
        self._flush_ticks = 0
        self._progress_ticks = 0
        self._closing = False
        self._pump()

    def _set_palette(self) -> None:
        """Replace the shared live palette with this window's selected theme."""
        COLORS.clear()
        COLORS.update(DARK_COLORS if self.theme == THEME_DARK else LIGHT_COLORS)

    # ------------------------------------------------------------- chrome

    def _build_window(self) -> None:
        self.root.title(APP_NAME)
        ui = self.ws.data.get("ui") or {}
        self.root.geometry(ui.get("geometry") or "1180x760")
        self.root.minsize(880, 560)
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        # The native Vista theme does not honour Treeview's field background on
        # Windows, leaving an especially jarring white empty area in dark mode.
        # Clam is fully colourable, while Vista remains the familiar light UI.
        if self.theme == THEME_DARK and "clam" in style.theme_names():
            style.theme_use("clam")
        elif "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=(self.ui_font, 10))
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure(
            "Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"], font=(self.ui_font, 9)
        )
        style.configure(
            "Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(self.ui_font, 13, "bold")
        )
        style.configure(
            "Section.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(self.ui_font, 11, "bold")
        )
        style.configure("Treeview", rowheight=26, font=(self.ui_font, 10), background=COLORS["panel"],
                        fieldbackground=COLORS["panel"], foreground=COLORS["text"], bordercolor=COLORS["border"],
                        lightcolor=COLORS["border"], darkcolor=COLORS["border"])
        style.map("Treeview", background=[("selected", COLORS["select"])], foreground=[("selected", COLORS["text"])])
        style.configure("TButton", background=COLORS["panel"], foreground=COLORS["text"], bordercolor=COLORS["border"])
        style.map("TButton", background=[("active", COLORS["select"])], foreground=[("disabled", COLORS["muted"])])
        style.configure("TEntry", fieldbackground=COLORS["panel"], foreground=COLORS["text"], bordercolor=COLORS["border"])
        style.configure("TCombobox", fieldbackground=COLORS["panel"], foreground=COLORS["text"], background=COLORS["panel"], bordercolor=COLORS["border"])
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["panel"])], foreground=[("readonly", COLORS["text"])])
        style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TRadiobutton", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TPanedwindow", background=COLORS["border"])
        style.configure("TScrollbar", background=COLORS["panel"], troughcolor=COLORS["bg"], bordercolor=COLORS["border"], arrowcolor=COLORS["muted"])
        style.configure("Toolbar.TButton", padding=(6, 3))
        style.configure(
            "Running.TLabel", background=COLORS["bg"], foreground=COLORS["accent"],
            font=(self.ui_font, 9, "bold"),
        )
        style.configure(
            "Warn.TLabel", background=COLORS["warn_bg"], foreground=COLORS["notice"],
            font=(self.ui_font, 9), padding=(8, 4),
        )

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        self._menus: list[tk.Menu] = [menubar]

        file_menu = tk.Menu(menubar, tearoff=0)
        self._menus.append(file_menu)
        file_menu.add_command(label="新增最上層專案", command=lambda: self.new_project(top_level=True))
        file_menu.add_command(label="新增子專案", command=lambda: self.new_project(top_level=False))
        file_menu.add_command(label="新增對話", command=self.new_conversation)
        file_menu.add_separator()
        file_menu.add_command(label="匯出選取的專案／對話…", command=self.export_selection)
        file_menu.add_command(label="匯出整個工作區…", command=self.export_workspace)
        file_menu.add_command(label="匯出為 Markdown…", command=self.export_markdown)
        file_menu.add_command(label="匯入…", command=self.import_archive)
        file_menu.add_separator()
        file_menu.add_command(label="開啟工作區資料夾", command=self.open_workspace_folder)
        file_menu.add_command(label="Agent 設定…", command=self.edit_agents)
        file_menu.add_command(label="預設值設定…", command=self.edit_defaults)
        file_menu.add_separator()
        file_menu.add_command(label="離開", command=self.on_close)
        menubar.add_cascade(label="檔案", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        self._menus.append(edit_menu)
        edit_menu.add_command(label="重新命名  (F2)", command=self.rename_selected)
        edit_menu.add_command(label="刪除  (Del)", command=self.delete_selected)
        edit_menu.add_separator()
        edit_menu.add_command(label="複製對話內容", command=self.copy_transcript)
        edit_menu.add_command(label="從這裡分岔出新對話", command=self.fork_conversation)
        edit_menu.add_command(label="審查未提交的變更", command=self.review_changes)
        edit_menu.add_command(label="重設對話（清空並開新 thread）", command=self.reset_conversation)
        menubar.add_cascade(label="編輯", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        self._menus.append(view_menu)
        self.tool_display_var = tk.StringVar(value=self.tool_display)
        for mode, label in TOOL_DISPLAY_LABELS:
            view_menu.add_radiobutton(
                label=label, value=mode, variable=self.tool_display_var,
                command=self.apply_tool_display,
            )
        view_menu.add_separator()
        self.show_outline_var = tk.BooleanVar(value=self.show_outline)
        self.show_info_var = tk.BooleanVar(value=self.show_info)
        view_menu.add_checkbutton(label="顯示對話大綱（左）",
                                  variable=self.show_outline_var,
                                  command=self.apply_panel_prefs)
        view_menu.add_checkbutton(label="顯示資訊面板（右）",
                                  variable=self.show_info_var,
                                  command=self.apply_panel_prefs)
        self.limit_width_var = tk.BooleanVar(value=self.limit_width)
        view_menu.add_checkbutton(label="限制逐字稿行寬", variable=self.limit_width_var,
                                  command=self.apply_panel_prefs)
        view_menu.add_separator()
        self.theme_var = tk.StringVar(value=self.theme)
        view_menu.add_radiobutton(label="淺色模式", value=THEME_LIGHT,
                                  variable=self.theme_var, command=self.apply_theme)
        view_menu.add_radiobutton(label="深色模式", value=THEME_DARK,
                                  variable=self.theme_var, command=self.apply_theme)
        menubar.add_cascade(label="檢視", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        self._menus.append(help_menu)
        help_menu.add_command(label="關於", command=self.show_about)
        menubar.add_cascade(label="說明", menu=help_menu)

        self.root.config(menu=menubar)
        self._configure_menus()
        self.root.bind_all("<Control-f>", lambda e: self.focus_search())
        self.root.bind_all("<Control-F>", lambda e: self.focus_search())

    # ------------------------------------------------------------- layout

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="TFrame")
        outer.pack(fill="both", expand=True)

        self.paned = ttk.PanedWindow(outer, orient="horizontal")
        self.paned.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self._build_tree_pane()
        self._build_detail_pane()

        ui = self.ws.data.get("ui") or {}
        # Deferred so the panes have a size to divide. Tracked because closing
        # the window sooner than this would fire it against a dead interpreter.
        self._sash_job = self.root.after(80, lambda: self._set_sash(ui.get("sash", 300)))

        self.status = ttk.Label(outer, text="", style="Muted.TLabel", anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 6))
        version = codex_runner.codex_version()
        self.set_status(f"就緒 · {version}" if version else "找不到 codex CLI，請確認已安裝並在 PATH 中")

    def _set_sash(self, position: int) -> None:
        try:
            self.paned.sashpos(0, int(position))
        except tk.TclError:
            pass

    def _build_tree_pane(self) -> None:
        left = ttk.Frame(self.paned, style="TFrame")
        self.paned.add(left, weight=0)

        bar = ttk.Frame(left, style="TFrame")
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="專案", style="Section.TLabel").pack(side="left")
        # Packed right-to-left, so the tuple is reversed to read left-to-right
        # on screen: ＋專案  ＋子專案  ＋對話.
        for text, tip, cmd in (
            ("＋對話", "在選取的專案下新增對話", self.new_conversation),
            ("＋子專案", "在選取的專案下新增子專案", lambda: self.new_project(top_level=False)),
            ("＋專案", "新增最上層專案", lambda: self.new_project(top_level=True)),
        ):
            btn = ttk.Button(bar, text=text, style="Toolbar.TButton", command=cmd, width=len(text) + 2)
            btn.pack(side="right", padx=(4, 0))
            _Tooltip(btn, tip)

        search_row = tk.Frame(left, bg=COLORS["border"])
        search_row.pack(fill="x", pady=(0, 6))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            search_row, textvariable=self.search_var, bd=0, bg=COLORS["panel"],
            fg=COLORS["text"], font=(self.ui_font, 9), insertbackground=COLORS["text"],
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=3)
        self.search_clear = tk.Button(
            search_row, text="✕", bd=0, bg=COLORS["panel"], fg=COLORS["muted"],
            font=(self.ui_font, 8), cursor="hand2", padx=6,
            command=self.clear_search,
        )
        self.search_clear.pack(side="right", pady=3, padx=(0, 1))
        self.search_placeholder = ttk.Label(
            search_row, text="搜尋專案／對話／內容", style="Muted.TLabel"
        )
        self.search_placeholder.place(x=8, y=4)
        self.search_entry.bind("<KeyRelease>", self._on_search_typed)
        self.search_entry.bind("<Escape>", lambda e: self.clear_search())
        self._search_job: str | None = None
        self.search_query = ""

        holder = ttk.Frame(left, style="TFrame")
        holder.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(holder, show="tree", selectmode="browse")
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("project", foreground=COLORS["text"])
        self.tree.tag_configure("conversation", foreground=COLORS["tree_conversation"])
        self.tree.tag_configure("running", foreground=COLORS["accent"])
        self.tree.tag_configure("droptarget", background=COLORS["drop_target"])

        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<<TreeviewOpen>>", self.on_tree_open_close)
        self.tree.bind("<<TreeviewClose>>", self.on_tree_open_close)
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<Button-3>", self.on_tree_context_menu)
        self.tree.bind("<ButtonPress-1>", self.on_drag_press)
        self.tree.bind("<B1-Motion>", self.on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self.on_drag_release)
        self.tree.bind("<F2>", lambda e: self.rename_selected())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())

    def _build_detail_pane(self) -> None:
        right = ttk.Frame(self.paned, style="TFrame")
        self.paned.add(right, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.conv_view = ConversationView(right, self)
        self.proj_view = ProjectView(right, self)
        self.empty_view = ttk.Frame(right, style="TFrame")
        ttk.Label(
            self.empty_view,
            text="在左側選擇一個專案或對話。",
            style="Muted.TLabel",
        ).pack(pady=40)
        for view in (self.conv_view, self.proj_view, self.empty_view):
            view.grid(row=0, column=0, sticky="nsew")
        self.empty_view.tkraise()

    # --------------------------------------------------------------- tree

    # -------------------------------------------------------------- search

    def _on_search_typed(self, _event=None) -> None:
        """Debounced: filtering scans transcripts, so not on every keystroke."""
        if self.search_var.get():
            self.search_placeholder.place_forget()
        else:
            self.search_placeholder.place(x=8, y=4)
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(250, self._apply_search)

    def focus_search(self) -> None:
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def _apply_search(self) -> None:
        self._search_job = None
        self.search_query = self.search_var.get().strip().casefold()
        self.refresh_tree()
        if self.search_query:
            matches = sum(1 for node, _ in self.ws.walk() if self._matches(node))
            self.set_status(f"搜尋「{self.search_var.get().strip()}」：{matches} 個項目符合")

    def clear_search(self) -> None:
        self.search_var.set("")
        self.search_placeholder.place(x=8, y=4)
        self.search_query = ""
        self.refresh_tree()

    def _matches(self, node: dict[str, Any]) -> bool:
        """A node matches on its own name, or on its transcript."""
        query = self.search_query
        if not query:
            return True
        if query in node["name"].casefold():
            return True
        if node["kind"] == store.CONVERSATION:
            return any(query in (m.get("text") or "").casefold()
                       for m in node["messages"])
        return False

    def _subtree_matches(self, node: dict[str, Any]) -> bool:
        if self._matches(node):
            return True
        if node["kind"] == store.PROJECT:
            return any(self._subtree_matches(child) for child in node["children"])
        return False

    def refresh_tree(self, select: str | None = None) -> None:
        selected = select or self.current_id
        self.tree.delete(*self.tree.get_children())
        filtering = bool(self.search_query)

        def insert(nodes, parent_iid, forced=False):
            for node in nodes:
                # A node whose own name matched brings its whole subtree with it,
                # so searching a project name shows what is inside it.
                hit = forced or not filtering or self._matches(node)
                if not hit and not self._subtree_matches(node):
                    continue
                self.tree.insert(
                    parent_iid,
                    "end",
                    iid=node["id"],
                    text=self._node_label(node),
                    tags=self._node_tags(node),
                    # While filtering, open everything so hits are visible.
                    open=True if filtering else bool(node.get("expanded", True)),
                )
                if node["kind"] == store.PROJECT:
                    insert(node["children"], node["id"], forced=hit and filtering)

        insert(self.ws.projects, "")
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.see(selected)

    def _node_label(self, node: dict[str, Any]) -> str:
        if node["kind"] == store.PROJECT:
            count = sum(1 for c in node["children"] if c["kind"] == store.CONVERSATION)
            suffix = f"  ({count})" if count else ""
            return f"📁  {node['name']}{suffix}"
        if node["id"] in self.turns:
            mark = "⏳"
        elif node.get("fork_of") and not node.get("thread_id"):
            mark = "🌿"  # branched, but its own thread starts on the first send
        else:
            mark = "💬"
        return f"{mark}  {node['name']}"

    def _node_tags(self, node: dict[str, Any]) -> tuple[str, ...]:
        if node["kind"] == store.PROJECT:
            return ("project",)
        return ("running",) if node["id"] in self.turns else ("conversation",)

    def refresh_node_label(self, node_id: str) -> None:
        node = self.ws.find(node_id)
        if node is not None and self.tree.exists(node_id):
            self.tree.item(node_id, text=self._node_label(node), tags=self._node_tags(node))

    def selected_id(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def target_id(self) -> str | None:
        """What a new node should attach to.

        Falls back to the last selected node, because a search filter can hide
        the selection from the tree without changing what you are working on.
        """
        return self.selected_id() or self.current_id

    def _select(self, node_id: str) -> None:
        """Select a row and refresh the detail pane.

        Treeview fires no <<TreeviewSelect>> when the row is already selected,
        so the refresh is invoked explicitly.
        """
        if not self.tree.exists(node_id):
            return
        self.tree.selection_set(node_id)
        self.tree.focus(node_id)
        self.tree.see(node_id)
        self.on_tree_select()

    def on_tree_select(self, _event=None) -> None:
        node_id = self.selected_id()
        self.current_id = node_id
        node = self.ws.find(node_id)
        if node is None:
            self.empty_view.tkraise()
        elif node["kind"] == store.CONVERSATION:
            self.conv_view.show(node)
            self.conv_view.tkraise()
        else:
            self.proj_view.show(node)
            self.proj_view.tkraise()

    def on_tree_open_close(self, _event=None) -> None:
        """Record which projects are open, reading the widget rather than the event.

        `<<TreeviewOpen>>`/`<<TreeviewClose>>` carry no item, and clicking the
        expander does not move the focus — so using `tree.focus()` recorded the
        clicked node's state against whichever node happened to be focused.
        Syncing every visible project instead is immune to that.
        """
        if self.search_query:
            return  # filtering force-opens everything; that is not a real state
        for node, _ in self.ws.walk():
            if node["kind"] == store.PROJECT and self.tree.exists(node["id"]):
                node["expanded"] = bool(self.tree.item(node["id"], "open"))
        self.ws.touch()

    def on_tree_double_click(self, event) -> None:
        node_id = self.tree.identify_row(event.y)
        if not node_id:
            return
        node = self.ws.find(node_id)
        if node is not None and node["kind"] == store.CONVERSATION:
            self.conv_view.focus_input()

    def on_tree_context_menu(self, event) -> None:
        node_id = self.tree.identify_row(event.y)
        if node_id:
            self.tree.selection_set(node_id)
            self.tree.focus(node_id)
        node = self.ws.find(node_id)

        menu = tk.Menu(self.root, tearoff=0)
        if node is not None and node["kind"] == store.PROJECT:
            menu.add_command(label="新增對話", command=self.new_conversation)
            menu.add_command(label="新增子專案", command=lambda: self.new_project(top_level=False))
            menu.add_separator()
            menu.add_command(label="審查未提交的變更", command=self.review_changes)
            menu.add_separator()
            menu.add_command(label="專案設定", command=lambda: self._select(node_id))
            menu.add_separator()
            menu.add_command(label="匯出這個專案…", command=self.export_selection)
            menu.add_command(label="匯出為 Markdown…", command=self.export_markdown)
            menu.add_command(label="匯入到這個專案…", command=self.import_archive)
        elif node is not None:
            menu.add_command(
                label="從這裡分岔出新對話",
                command=self.fork_conversation,
                state="normal" if node.get("thread_id") else "disabled",
            )
            menu.add_separator()
            menu.add_command(label="重設對話（清空並開新 thread）", command=self.reset_conversation)
            menu.add_command(label="複製對話內容", command=self.copy_transcript)
            if node_id in self.turns:
                menu.add_command(label="停止執行", command=lambda: self.stop_turn(node_id))
            menu.add_separator()
            menu.add_command(label="匯出這個對話…", command=self.export_selection)
            menu.add_command(label="匯出為 Markdown…", command=self.export_markdown)
        if node is not None:
            menu.add_separator()
        menu.add_command(label="新增最上層專案", command=lambda: self.new_project(top_level=True))
        if node is None:
            # Right-clicked empty space: the only sensible transfer is a
            # top-level import, and exporting everything.
            menu.add_command(label="匯出整個工作區…", command=self.export_workspace)
            menu.add_command(label="匯入到最上層…",
                             command=lambda: self.import_archive(top_level=True))
        if node is not None:
            menu.add_separator()
            menu.add_command(label="重新命名", command=self.rename_selected)
            menu.add_command(label="刪除", command=self.delete_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ------------------------------------------------------- drag & drop

    def on_drag_press(self, event) -> None:
        self._drag_id = self.tree.identify_row(event.y)
        self._drag_started = False
        self._drag_origin_y = event.y

    def on_drag_motion(self, event) -> None:
        if not self._drag_id:
            return
        if not self._drag_started and abs(event.y - self._drag_origin_y) < 6:
            return
        self._drag_started = True
        self.tree.configure(cursor="hand2")
        for iid in self.tree.tag_has("droptarget"):
            self.refresh_node_label(iid)
        target = self.tree.identify_row(event.y)
        if target and target != self._drag_id:
            tags = tuple(self.tree.item(target, "tags")) + ("droptarget",)
            self.tree.item(target, tags=tags)

    def on_drag_release(self, event) -> None:
        drag_id, started = self._drag_id, self._drag_started
        self._drag_id, self._drag_started = None, False
        self.tree.configure(cursor="")
        for iid in list(self.tree.tag_has("droptarget")):
            self.refresh_node_label(iid)
        if not drag_id or not started:
            return

        target_id = self.tree.identify_row(event.y)
        node = self.ws.find(drag_id)
        if node is None:
            return

        if not target_id:
            ok = self.ws.move(drag_id, None)  # dropped on empty space -> top level
            if not ok:
                self.set_status("對話必須放在專案底下")
            self.refresh_tree(drag_id)
            return

        if target_id == drag_id:
            return

        target = self.ws.find(target_id)
        if target is None:
            return
        if target["kind"] == store.PROJECT:
            ok = self.ws.move(drag_id, target_id)
        else:
            parent = self.ws.parent_of(target_id)
            index = next(
                (i for i, n in enumerate(parent["children"]) if n["id"] == target_id), None
            )
            ok = self.ws.move(drag_id, parent["id"], index)
        if not ok:
            self.set_status("無法移到該位置（不能把專案放進自己的子專案）")
        self.refresh_tree(drag_id)

    # ----------------------------------------------------------- commands

    def new_project(self, top_level: bool) -> None:
        parent_id = None
        if not top_level:
            project = self.ws.owning_project(self.target_id())
            if project is None:
                messagebox.showinfo(APP_NAME, "請先選一個專案，才能在它底下新增子專案。")
                return
            parent_id = project["id"]
        name = simpledialog.askstring(
            "新增專案", "專案名稱：", initialvalue="新專案", parent=self.root
        )
        if not name:
            return
        node = self.ws.add_project(parent_id, name.strip())
        self._reveal_new(node["id"])

    def new_conversation(self) -> None:
        project = self.ws.owning_project(self.target_id())
        if project is None:
            messagebox.showinfo(APP_NAME, "請先選一個專案或它底下的對話。")
            return
        # No name prompt: a conversation is identified by its content, and being
        # asked to name it before you know what it is about is pure friction.
        # `unique_name` keeps the siblings distinguishable, F2 renames later.
        node = self.ws.add_conversation(
            project["id"], self.ws.unique_name(project, "新對話")
        )
        self._reveal_new(node["id"])
        self.conv_view.focus_input()
        self.set_status(f"已在「{project['name']}」新增「{node['name']}」· F2 可改名")

    def _reveal_new(self, node_id: str) -> None:
        """Select a freshly created node, lifting any search filter hiding it."""
        if self.search_query:
            self.clear_search()
        self.refresh_tree(node_id)
        self._select(node_id)

    def rename_selected(self) -> None:
        node_id = self.selected_id()
        node = self.ws.find(node_id)
        if node is None:
            return
        name = simpledialog.askstring(
            "重新命名", "新名稱：", initialvalue=node["name"], parent=self.root
        )
        if not name:
            return
        self.ws.rename(node_id, name.strip())
        self.refresh_node_label(node_id)
        self.on_tree_select()

    def delete_selected(self) -> None:
        node_id = self.selected_id()
        node = self.ws.find(node_id)
        if node is None:
            return
        if node["kind"] == store.PROJECT:
            children = sum(1 for _ in _iter_subtree(node)) - 1
            extra = f"，含 {children} 個子項目" if children else ""
            question = f"刪除專案「{node['name']}」{extra}？此動作無法復原。"
        else:
            question = f"刪除對話「{node['name']}」？此動作無法復原。"
        if not messagebox.askyesno(APP_NAME, question, parent=self.root):
            return
        threads = []
        for sub in _iter_subtree(node):
            self._retire_turn(sub["id"])
            self.conv_view.drop_queue(sub["id"])
            if sub.get("thread_id"):
                threads.append(sub["thread_id"])

        # Work out where to land before the node disappears: the next sibling,
        # else the previous one, else the parent. Leaving nothing selected made
        # the detail pane go blank and the tree lose its place.
        siblings = self.ws.siblings_of(node_id)
        parent = self.ws.parent_of(node_id)
        index = next(i for i, n in enumerate(siblings) if n["id"] == node_id)
        neighbours = siblings[index + 1:] + list(reversed(siblings[:index]))
        successor = neighbours[0]["id"] if neighbours else (parent["id"] if parent else None)

        self.ws.delete(node_id)
        # Archive rather than orphan: the Codex session outlives our node, and
        # `codex unarchive` can still bring it back if this was a mistake.
        for thread_id in threads:
            codex_runner.archive_session(thread_id)
        if threads:
            self.set_status(
                f"已刪除，並歸檔 {len(threads)} 個 Codex session（可用 codex unarchive 還原）"
            )
        self.current_id = None
        self.refresh_tree()
        if successor and self.tree.exists(successor):
            self._select(successor)
        else:
            self.empty_view.tkraise()

    def fork_conversation(self) -> None:
        """Branch the selected conversation into a sibling that shares its history."""
        node_id = self.selected_id()
        node = self.ws.find(node_id)
        if node is None or node["kind"] != store.CONVERSATION:
            messagebox.showinfo(APP_NAME, "請先選一個對話。")
            return
        if self.ws.conversation_agent(node_id) != store.CODEX_AGENT:
            messagebox.showinfo(APP_NAME, "分岔對話目前只支援 Codex CLI。")
            return
        if node_id in self.turns:
            messagebox.showinfo(APP_NAME, "這個對話還在執行中，請等回覆完成後再分岔。")
            return
        if not node.get("thread_id"):
            messagebox.showinfo(
                APP_NAME,
                "這個對話還沒送出過任何訊息，沒有可以分岔的內容。\n"
                "先送一則訊息，之後就能從它分岔出新對話。",
            )
            return

        forked = self.ws.fork_conversation(node_id)
        if forked is None:
            self.set_status("無法分岔這個對話")
            return
        self._reveal_new(forked["id"])
        self.conv_view.focus_input()
        self.set_status(
            f"已從「{node['name']}」分岔出「{forked['name']}」·"
            " 送出第一則訊息時才會真的建立新的 Codex thread"
        )

    def reset_conversation(self) -> None:
        node_id = self.selected_id()
        node = self.ws.find(node_id)
        if node is None or node["kind"] != store.CONVERSATION:
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"清空「{node['name']}」的內容並開一個新的 Agent session？",
            parent=self.root,
        ):
            return
        if node_id in self.turns:
            self.stop_turn(node_id)
        # Retire the turn before wiping: a killed turn can still have events in
        # the queue, and replaying them would put the old thread id and messages
        # straight back into the conversation we just cleared.
        self._retire_turn(node_id)
        self.conv_view.drop_queue(node_id)
        self.ws.clear_thread(node_id)
        self.refresh_node_label(node_id)
        self.conv_view.show(node)
        self.set_status(f"「{node['name']}」已重設，下次送出會開一個新的 Agent session")

    def apply_panel_prefs(self) -> None:
        self.show_outline = bool(self.show_outline_var.get())
        self.show_info = bool(self.show_info_var.get())
        self.limit_width = bool(self.limit_width_var.get())
        ui = self.ws.data.setdefault("ui", {})
        ui["show_outline"] = self.show_outline
        ui["show_info"] = self.show_info
        ui["limit_width"] = self.limit_width
        self.ws.touch()
        self.conv_view.apply_panels()
        # Toggling only the cap changes no geometry, so no <Configure> follows;
        # re-measure directly rather than scheduling work that could outlive the
        # window.
        self.conv_view._text_pad = -1
        self.conv_view._fit_text_measure()

    def _configure_menus(self) -> None:
        """Menus are classic Tk widgets, so ttk styles do not reach them."""
        for menu in getattr(self, "_menus", []):
            menu.configure(
                background=COLORS["panel"], foreground=COLORS["text"],
                activebackground=COLORS["select"], activeforeground=COLORS["text"],
                disabledforeground=COLORS["muted"], selectcolor=COLORS["accent"],
                borderwidth=0, activeborderwidth=0,
            )

    def _replace_widget_colors(self, widget: tk.Misc, old: dict[str, str]) -> None:
        """Update classic Tk widget options created before a live theme switch."""
        replacements = {old[key]: COLORS[key] for key in old if key in COLORS}
        for option in (
            "background", "foreground", "activebackground", "activeforeground",
            "insertbackground", "selectbackground", "selectforeground",
            "inactiveselectbackground",
        ):
            try:
                value = str(widget.cget(option))
                replacement = replacements.get(value)
                if replacement:
                    widget.configure(**{option: replacement})
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._replace_widget_colors(child, old)

    def apply_theme(self) -> None:
        """Apply and persist the View-menu theme without disturbing the transcript."""
        selected = self.theme_var.get()
        if selected not in (THEME_LIGHT, THEME_DARK) or selected == self.theme:
            return
        old = dict(COLORS)
        self.theme = selected
        self._set_palette()
        self.root.configure(bg=COLORS["bg"])
        self._build_styles()
        self._configure_menus()
        self._replace_widget_colors(self.root, old)
        self.tree.tag_configure("project", foreground=COLORS["text"])
        self.tree.tag_configure("conversation", foreground=COLORS["tree_conversation"])
        self.tree.tag_configure("running", foreground=COLORS["accent"])
        self.tree.tag_configure("droptarget", background=COLORS["drop_target"])
        self.conv_view._configure_tags()
        self.ws.data.setdefault("ui", {})["theme"] = self.theme
        self.ws.touch()
        self.set_status("已切換為" + ("深色模式" if self.theme == THEME_DARK else "淺色模式"))

    def apply_tool_display(self) -> None:
        """Switch how command executions are drawn and redraw the transcript."""
        self.tool_display = self.tool_display_var.get()
        self.ws.data.setdefault("ui", {})["tool_display"] = self.tool_display
        self.ws.touch()
        conv = self.ws.find(self.current_id)
        if conv is not None and conv["kind"] == store.CONVERSATION:
            offset = self.conv_view.text.yview()[0]
            self.conv_view.show(conv)
            self.conv_view.text.yview_moveto(offset)
        self.set_status(
            "工具輸出：" + dict(TOOL_DISPLAY_LABELS).get(self.tool_display, self.tool_display)
        )

    def copy_transcript(self) -> None:
        node = self.ws.find(self.selected_id())
        if node is None or node["kind"] != store.CONVERSATION:
            return
        lines = [
            f"[{ROLE_LABELS.get(m['role'], m['role'])}] {m['text']}" for m in node["messages"]
        ]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n\n".join(lines))
        self.set_status("已複製對話內容到剪貼簿")

    # ----------------------------------------------------- export / import

    ARCHIVE_TYPES = [("Tree Agent 匯出檔", "*.zip"), ("所有檔案", "*.*")]

    def _report_export(self, summary: dict[str, Any]) -> None:
        message = (
            f"已匯出到：\n{summary['path']}\n\n"
            f"{summary['projects']} 個專案 · {summary['conversations']} 個對話 · "
            f"{summary['attachments']} 個附件"
        )
        missing = summary.get("missing_attachments") or []
        if missing:
            message += f"\n\n有 {len(missing)} 個附件檔案已不存在，未收進匯出檔。"
        messagebox.showinfo(APP_NAME, message, parent=self.root)
        self.set_status(f"已匯出 {summary['conversations']} 個對話到 {summary['path']}")

    def export_selection(self) -> None:
        node = self.ws.find(self.selected_id())
        if node is None:
            messagebox.showinfo(APP_NAME, "請先選一個專案或對話。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="匯出選取的專案／對話",
            defaultextension=".zip", filetypes=self.ARCHIVE_TYPES,
            initialfile=_safe_filename(node["name"]) + ".zip",
        )
        if not path:
            return
        try:
            self._report_export(transfer.export_nodes(self.ws, [node["id"]], path))
        except (transfer.TransferError, OSError) as exc:
            messagebox.showerror(APP_NAME, f"匯出失敗：\n{exc}", parent=self.root)

    def export_workspace(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="匯出整個工作區",
            defaultextension=".zip", filetypes=self.ARCHIVE_TYPES,
            initialfile="tree-agent-workspace.zip",
        )
        if not path:
            return
        try:
            self._report_export(transfer.export_workspace(self.ws, path))
        except (transfer.TransferError, OSError) as exc:
            messagebox.showerror(APP_NAME, f"匯出失敗：\n{exc}", parent=self.root)

    def export_markdown(self) -> None:
        node = self.ws.find(self.selected_id())
        if node is None:
            messagebox.showinfo(APP_NAME, "請先選一個專案或對話。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="匯出為 Markdown", defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("所有檔案", "*.*")],
            initialfile=_safe_filename(node["name"]) + ".md",
        )
        if not path:
            return
        try:
            summary = transfer.export_markdown(self.ws, node["id"], path)
        except (transfer.TransferError, OSError) as exc:
            messagebox.showerror(APP_NAME, f"匯出失敗：\n{exc}", parent=self.root)
            return
        self.set_status(
            f"已匯出 {summary['conversations']} 個對話為 Markdown：{summary['path']}"
        )

    def import_archive(self, top_level: bool = False) -> None:
        """Import an archive. `top_level` ignores the selection, for the
        empty-space context menu whose label promises exactly that."""
        path = filedialog.askopenfilename(
            parent=self.root, title="匯入", filetypes=self.ARCHIVE_TYPES
        )
        if not path:
            return
        target = None if top_level else self.ws.owning_project(self.selected_id())
        where = f"「{target['name']}」底下" if target else "最上層"
        try:
            manifest = transfer.read_manifest(path)
        except transfer.TransferError as exc:
            messagebox.showerror(APP_NAME, f"匯入失敗：\n{exc}", parent=self.root)
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"要把這個匯出檔匯入到 {where} 嗎？\n\n"
            f"來源電腦：{manifest.get('source_host') or '未記錄'}\n"
            f"匯出時間：{manifest.get('exported_at') or '未記錄'}\n"
            f"最上層項目：{len(manifest['nodes'])} 個\n\n"
            "匯入會新增項目，不會覆蓋現有內容。",
            parent=self.root,
        ):
            return
        try:
            summary = transfer.import_archive(
                self.ws, path, target["id"] if target else None
            )
        except (transfer.TransferError, OSError) as exc:
            messagebox.showerror(APP_NAME, f"匯入失敗：\n{exc}", parent=self.root)
            return

        if summary["roots"]:
            self._reveal_new(summary["roots"][0])
        else:
            self.refresh_tree()
        note = (
            f"已匯入 {summary['projects']} 個專案 · {summary['conversations']} 個對話 · "
            f"{summary['attachments']} 個附件。"
        )
        if summary["foreign_host"] and summary["threads"]:
            note += (
                f"\n\n其中 {summary['threads']} 個對話帶有來自「{summary['source_host']}」"
                "的 Codex thread。逐字稿可以照常閱讀，但要在這台電腦繼續對話，"
                "需要先「重設對話」開一個新的 thread。"
            )
        messagebox.showinfo(APP_NAME, note, parent=self.root)
        self.set_status(note.splitlines()[0])

    def open_workspace_folder(self) -> None:
        _open_in_explorer(self.ws.home)

    def edit_defaults(self) -> None:
        DefaultsDialog(self.root, self)

    def edit_agents(self) -> None:
        AgentsDialog(self.root, self)

    def show_about(self) -> None:
        messagebox.showinfo(
            APP_NAME,
            f"{APP_NAME}\n\n"
            "為 Codex CLI 加上樹狀專案結構的 Windows GUI。\n"
            "專案可無限層嵌套，每個對話有自己的 Codex thread。\n\n"
            f"Codex: {codex_runner.codex_version() or '未偵測到'}\n"
            f"工作區: {self.ws.path}",
            parent=self.root,
        )

    # ------------------------------------------------------------- turns

    def review_changes(self) -> None:
        """Run `codex exec review --uncommitted` into a fresh conversation."""
        project = self.ws.owning_project(self.selected_id())
        if project is None:
            messagebox.showinfo(APP_NAME, "請先選一個專案或它底下的對話。")
            return
        settings = self.ws.resolve(project["id"])
        cwd = settings["cwd"]
        if not os.path.isdir(os.path.join(cwd, ".git")):
            if not messagebox.askyesno(
                APP_NAME,
                f"{cwd}\n\n看起來不是 git 儲存庫的根目錄，審查可能找不到變更。\n仍要繼續嗎？",
                parent=self.root,
            ):
                return
        conv = self.ws.add_conversation(
            project["id"], self.ws.unique_name(project, "程式碼審查")
        )
        self._reveal_new(conv["id"])
        self.send(
            conv["id"],
            "審查未提交的變更（codex exec review --uncommitted）",
            review=codex_runner.REVIEW_UNCOMMITTED,
        )

    def send(
        self,
        conv_id: str,
        prompt: str,
        images: list[str] | None = None,
        review: str | None = None,
    ) -> None:
        conv = self.ws.find(conv_id)
        if conv is None or conv["kind"] != store.CONVERSATION:
            return
        if conv_id in self.turns:
            messagebox.showinfo(APP_NAME, "這個對話還在執行中，請先等它完成或按停止。")
            return

        images = list(images or ())
        settings = self.ws.resolve(conv_id)
        agent_id = self.ws.conversation_agent(conv_id)
        if agent_id == store.CLAUDE_AGENT and images:
            messagebox.showinfo(APP_NAME, "Claude Code 的第一版尚不支援圖片附件，請移除附件後再送出。")
            return

        # Project instructions ride along with the first message of a new
        # thread; from then on the thread itself carries them, so resending
        # would just burn tokens. A fork already inherits its source's context.
        instructions = ""
        has_context = conv.get("claude_session_id") if agent_id == store.CLAUDE_AGENT else (conv.get("thread_id") or conv.get("fork_of"))
        if not has_context:
            instructions = self.ws.instructions_for(conv_id)
        outgoing = f"{instructions}\n\n---\n\n{prompt}" if instructions else prompt
        # The file names come from the message metadata rather than being
        # baked into its text, so the transcript can show clickable previews.
        if instructions:
            note = f"已套用「{self.ws.path_of(conv_id)}」的專案提示詞（{len(instructions)} 字）"
            self.ws.append_message(conv_id, "meta", note)
            if conv_id == self.current_id:
                self.conv_view.append("meta", note)
        self.ws.append_message(conv_id, "user", prompt, images=images or None)
        if conv_id == self.current_id:
            self.conv_view.append("user", prompt, images)

        self._autoname(conv, prompt, images)

        self._turn_counter += 1
        serial = self._turn_counter
        self.turn_serials[conv_id] = serial
        self.turn_started[conv_id] = time.monotonic()
        self.turn_stage[conv_id] = "start"

        emit = lambda event, cid=conv_id, serial=serial: self.events.put((cid, serial, event))
        if agent_id == store.CLAUDE_AGENT:
            if review:
                messagebox.showinfo(APP_NAME, "程式碼審查功能目前只支援 Codex CLI。")
                return
            turn = codex_runner.ClaudeTurn(
                prompt=outgoing, cwd=settings["cwd"], emit=emit,
                session_id=conv.get("claude_session_id"), model=settings.get("model"),
                executable=self.ws.agent_path(store.CLAUDE_AGENT),
            )
        else:
            turn = codex_runner.Turn(
                prompt=outgoing, cwd=settings["cwd"], emit=emit,
                thread_id=conv.get("thread_id"), model=settings.get("model"),
                sandbox=settings.get("sandbox"), fork_from=conv.get("fork_of"),
                images=images, review=review,
                executable=self.ws.agent_path(store.CODEX_AGENT),
            )
        self.turns[conv_id] = turn
        self.refresh_node_label(conv_id)
        if conv_id == self.current_id:
            self.conv_view.set_running(True)
        if review:
            mode = f"review --{review}"
        elif agent_id == store.CLAUDE_AGENT and conv.get("claude_session_id"):
            mode = "resume"
        elif conv.get("thread_id"):
            mode = "resume"
        elif conv.get("fork_of"):
            mode = "fork"
        else:
            mode = "new thread"
        warning = codex_runner.sandbox_warning(settings["cwd"], settings.get("sandbox")) if agent_id == store.CODEX_AGENT else None
        if warning:
            self.set_status("⚠ " + warning)
        else:
            self.set_status(
                f"執行中（{AGENT_LABELS[agent_id]} · {mode}）· cwd={settings['cwd']}"
            )
        turn.start()

    def handle_slash_command(self, conv_id: str, command: str) -> None:
        """Handle the small set of app commands safe in non-interactive CLIs."""
        conv = self.ws.find(conv_id)
        if conv is None or conv["kind"] != store.CONVERSATION:
            return
        normalized = command.strip().lower()
        agent_id = self.ws.conversation_agent(conv_id)
        self.ws.append_message(conv_id, "user", command)
        if conv_id == self.current_id:
            self.conv_view.append("user", command)
        self._autoname(conv, command, [])
        if normalized == "/status":
            settings = self.ws.resolve(conv_id)
            executable = self.ws.agent_path(agent_id)
            version = (codex_runner.claude_version(executable) if agent_id == store.CLAUDE_AGENT
                       else codex_runner.codex_version(executable)) or "未偵測到"
            session = (conv.get("claude_session_id") if agent_id == store.CLAUDE_AGENT
                       else conv.get("thread_id")) or "尚未建立"
            text = (
                f"Agent: {AGENT_LABELS[agent_id]}\nCLI: {version}\n"
                f"工作目錄: {settings['cwd']}\n模型: {settings.get('model') or '(config 預設)'}\n"
                f"session: {session}\n工作區: {self.ws.path}"
            )
            self.ws.append_message(conv_id, "agent", text, agent_id=agent_id)
            if conv_id == self.current_id:
                self.conv_view.append("agent", text, agent_id=agent_id)
            self.set_status(f"已顯示 {AGENT_LABELS[agent_id]} 狀態")
            return
        text = (
            f"{AGENT_LABELS[agent_id]} 的「{command}」是互動式 CLI 指令，"
            "目前的非互動模式不支援。請在終端機執行 claude 後使用，"
            "或使用 Tree Agent 內建的 /status。"
        )
        self.ws.append_message(conv_id, "notice", text)
        if conv_id == self.current_id:
            self.conv_view.append("notice", text)
        self.set_status("此 slash command 僅支援 Claude 互動式 CLI")

    def _autoname(self, conv: dict[str, Any], prompt: str,
                  images: list[str]) -> None:
        """Title a still-unnamed conversation from its first message.

        Derived locally rather than asked of Codex: that would cost an extra
        turn, and the opening message is already the best summary of intent.
        Only placeholder names are touched, so a title you typed is never lost.
        """
        if not store.is_auto_name(conv["name"]):
            return
        if sum(1 for m in conv["messages"] if m["role"] == "user") > 1:
            return          # already had a first message; this is not it
        source = prompt
        if prompt == IMAGE_ONLY_PROMPT and images:
            source = os.path.basename(images[0])
        title = store.title_from(source)
        if len(title.strip()) < 2:
            return          # nothing worth showing; keep the placeholder
        parent = self.ws.parent_of(conv["id"])
        if parent is None:
            return
        self.ws.rename(conv["id"], self.ws.unique_name(parent, title))
        self.refresh_node_label(conv["id"])
        if conv["id"] == self.current_id:
            self.conv_view.refresh_header()

    def stop_turn(self, conv_id: str) -> None:
        turn = self.turns.get(conv_id)
        if turn is not None:
            turn.cancel()
            self.set_status("已送出停止訊號…")

    def _retire_turn(self, conv_id: str) -> None:
        """Drop a conversation's turn and make its queued events stale."""
        turn = self.turns.pop(conv_id, None)
        if turn is not None:
            turn.cancel()
        self.turn_serials.pop(conv_id, None)
        self.turn_started.pop(conv_id, None)
        self.turn_stage.pop(conv_id, None)

    def running_progress(self, conv_id: str) -> str:
        """"執行中 12s · 執行指令", or "" when this conversation is idle."""
        started = self.turn_started.get(conv_id)
        if conv_id not in self.turns or started is None:
            return ""
        stage = STAGE_LABELS.get(self.turn_stage.get(conv_id, "start"), "")
        elapsed = int(time.monotonic() - started)
        return f"⏳ 執行中 {elapsed}s · {stage}" if stage else f"⏳ 執行中 {elapsed}s"

    def _pump(self) -> None:
        """Drain worker-thread events on the UI thread.

        Wrapped so that a single bad event cannot break the `after` chain and
        silently freeze every running conversation.
        """
        if self._closing:
            return
        try:
            while True:
                conv_id, serial, event = self.events.get_nowait()
                if serial != self.turn_serials.get(conv_id):
                    continue  # left over from a cancelled, reset or deleted turn
                try:
                    self._handle_event(conv_id, event)
                except Exception as exc:  # noqa: BLE001 - keep the pump alive
                    self.set_status(f"處理事件時發生錯誤：{exc}")
        except queue.Empty:
            pass

        self._progress_ticks += 1
        if self._progress_ticks >= PROGRESS_TICKS:
            self._progress_ticks = 0
            self.conv_view.refresh_progress()

        self._flush_ticks += 1
        if self._flush_ticks >= FLUSH_EVERY_TICKS:
            self._flush_ticks = 0
            try:
                self.ws.flush()
            except OSError as exc:
                self.set_status(f"儲存失敗：{exc}")
        self._pump_job = self.root.after(PUMP_INTERVAL_MS, self._pump)

    def _handle_event(self, conv_id: str, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        visible = conv_id == self.current_id

        if kind == "thread":
            thread_id = event.get("thread_id")
            if thread_id:
                self.ws.set_thread_id(conv_id, thread_id)
                self.refresh_node_label(conv_id)  # 🌿 becomes 💬 once it has a thread
                if visible:
                    self.conv_view.refresh_header()
        elif kind == "session":
            session_id = event.get("session_id")
            conv = self.ws.find(conv_id)
            if session_id and conv is not None:
                conv["claude_session_id"] = session_id
                self.ws.save()
                if visible:
                    self.conv_view.refresh_header()
        elif kind == "turn_start":
            # Codex can think for a while before the first item arrives; without
            # this the label would sit on "starting" for all of it.
            self.turn_stage[conv_id] = "thinking"
        elif kind == "item":
            role, text = event.get("role", "agent"), event.get("text", "")
            self.turn_stage[conv_id] = {"tool": "tool", "agent": "writing",
                                        "reasoning": "thinking"}.get(role, "thinking")
            agent_id = self.ws.conversation_agent(conv_id)
            # Claude's stream emits one terse event per tool call. Keep those
            # events for auditability, but reserve the transcript for the
            # actual answer; the info rail has a dedicated scrollable log.
            if role == "tool" and agent_id == store.CLAUDE_AGENT:
                self.ws.append_message(conv_id, "agent_tool", text)
                if visible:
                    self.conv_view.refresh_info()
                return
            self.ws.append_message(conv_id, role, text, **({"agent_id": agent_id} if role == "agent" else {}))
            if visible:
                self.conv_view.append(role, text, agent_id=agent_id if role == "agent" else None)
            self.refresh_node_label(conv_id)
        elif kind == "usage":
            total = self.ws.add_usage(conv_id, event.get("usage") or {})
            if visible:
                self.conv_view.refresh_header()
            self.set_status(
                "完成 · 這回 in={} out={} · 此對話累計 in={} out={}（{} 回）".format(
                    (event.get("usage") or {}).get("input_tokens", "?"),
                    (event.get("usage") or {}).get("output_tokens", "?"),
                    total.get("input_tokens", 0),
                    total.get("output_tokens", 0),
                    total.get("turns", 0),
                )
            )
        elif kind == "log":
            text = event.get("text", "")
            if visible:
                self.conv_view.append_log(text)
        elif kind == "done":
            self.turns.pop(conv_id, None)
            self.turn_serials.pop(conv_id, None)
            self.turn_started.pop(conv_id, None)
            self.turn_stage.pop(conv_id, None)
            self.refresh_node_label(conv_id)
            started_next = self.conv_view.start_next_queued(conv_id)
            if visible and not started_next:
                self.conv_view.set_running(False)
            rc = event.get("returncode")
            if event.get("cancelled"):
                self.ws.append_message(conv_id, "meta", "（已停止）")
                if visible:
                    self.conv_view.append("meta", "（已停止）")
                self.set_status("已停止")
            elif rc not in (0, None):
                message = f"{AGENT_LABELS.get(self.ws.conversation_agent(conv_id), 'Agent')} 結束代碼 {rc}"
                self.ws.append_message(conv_id, "error", message)
                if visible:
                    self.conv_view.append("error", message)
                self.set_status(message)

    # ------------------------------------------------------------- misc

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _restore_selection(self) -> None:
        last = (self.ws.data.get("ui") or {}).get("selected")
        if last and self.tree.exists(last):
            self.tree.selection_set(last)
            self.tree.see(last)
        else:
            first = self.tree.get_children()
            if first:
                self.tree.selection_set(first[0])

    def on_close(self) -> None:
        if self.turns and not messagebox.askyesno(
            APP_NAME, f"還有 {len(self.turns)} 個對話在執行中，確定要關閉？", parent=self.root
        ):
            return
        self._closing = True
        self.conv_view.cancel_pending()
        for attr in ("_pump_job", "_sash_job", "_search_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        for turn in list(self.turns.values()):
            turn.cancel()
        ui = self.ws.data.setdefault("ui", {})
        ui["geometry"] = self.root.winfo_geometry()
        ui["selected"] = self.current_id
        ui["tool_display"] = self.tool_display
        ui["show_outline"] = self.show_outline
        ui["show_info"] = self.show_info
        ui["limit_width"] = self.limit_width
        self.conv_view._remember_rail_widths()
        ui["outline_width"] = self.outline_width
        ui["info_width"] = self.info_width
        try:
            ui["sash"] = self.paned.sashpos(0)
        except tk.TclError:
            pass
        try:
            self.ws.save()
        except OSError as exc:
            if not messagebox.askyesno(
                APP_NAME,
                f"儲存工作區失敗：\n{exc}\n\n仍要關閉嗎？",
                parent=self.root,
            ):
                self._closing = False
                self._pump()
                return
        if self.lock is not None:
            self.lock.release()
        self.root.destroy()


class ConversationView(ttk.Frame):
    """Transcript + composer for one conversation."""

    def __init__(self, master: tk.Widget, app: TreeAgentApp) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self.conv_id: str | None = None
        # Unsent text and attachments follow their own conversation instead of
        # leaking into the next one you click on.
        self.drafts: dict[str, str] = {}
        self.attachments: dict[str, list[str]] = {}
        # Prompts submitted while this conversation is running.  They are
        # intentionally in-memory like drafts: a queue is interaction state,
        # not a durable part of the transcript until it actually starts.
        self.queued: dict[str, list[tuple[str, list[str]]]] = {}
        # Tk keeps no reference to a PhotoImage, so inline previews must be held
        # here or every one of them blanks out.
        self._inline_images: list[tk.PhotoImage] = []
        self._link_targets: dict[str, str] = {}
        self._tool_blocks: list[tuple[str, str, str]] = []

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, style="TFrame")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(header, text="", style="Title.TLabel", anchor="w")
        self.title_label.grid(row=0, column=0, sticky="ew")
        self.meta_label = ttk.Label(header, text="", style="Muted.TLabel", anchor="w")
        self.meta_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.run_label = ttk.Label(header, text="", style="Running.TLabel", anchor="w")
        self.run_label.grid(row=1, column=1, sticky="e", padx=(10, 0))
        self.run_label.grid_remove()
        self.warn_label = ttk.Label(header, text="", style="Warn.TLabel", anchor="w")
        self.warn_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.warn_label.grid_remove()

        buttons = ttk.Frame(header, style="TFrame")
        buttons.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))
        ttk.Label(buttons, text="Agent", style="Muted.TLabel").pack(side="left", padx=(0, 4))
        self.agent_var = tk.StringVar(value=AGENT_LABELS[store.CODEX_AGENT])
        self.agent_picker = ttk.Combobox(
            buttons, textvariable=self.agent_var, state="readonly", width=13,
            values=tuple(AGENT_LABELS.values()),
        )
        self.agent_picker.pack(side="left", padx=(0, 8))
        self.agent_picker.bind("<<ComboboxSelected>>", self._on_agent_changed)
        ttk.Button(buttons, text="開啟工作目錄", style="Toolbar.TButton",
                   command=self.open_cwd).pack(side="left", padx=(0, 4))
        self.fork_button = ttk.Button(buttons, text="分岔", style="Toolbar.TButton",
                                      command=self.app.fork_conversation)
        self.fork_button.pack(side="left", padx=(0, 4))
        _Tooltip(self.fork_button, "從這裡分岔出新對話：共用目前的上下文，之後各走各的")
        ttk.Button(buttons, text="重設對話", style="Toolbar.TButton",
                   command=self.app.reset_conversation).pack(side="left")

        # A long project path or cwd would otherwise push the buttons off the
        # right edge; wrap the text to whatever space is left instead.
        self._header_buttons = buttons
        self._wrap = 0
        header.bind("<Configure>", self._on_header_resize)

        # A real paned window, so every boundary can be dragged.
        self.splitter = ttk.PanedWindow(self, orient="horizontal")
        self.splitter.grid(row=1, column=0, columnspan=3, sticky="nsew")
        self.splitter.bind("<ButtonRelease-1>", self._remember_rail_widths)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self.body = body = tk.Frame(self.splitter, bg=COLORS["border"],
                                    highlightthickness=0)
        self._panels_shown = None
        self.bind("<Configure>", self._fit_content_width)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        # The measure cap is driven by the body's own size, so it cannot race
        # with the pass that adds or removes panes.
        self._text_pad = -1
        self._settle_job: str | None = None
        body.bind("<Configure>", self._fit_text_measure)

        self.text = tk.Text(
            body,
            width=1, height=1,      # sized by the pane, not by its own request
            wrap="word",
            bd=0,
            padx=TEXT_INSET_PX,
            pady=12,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(app.ui_font, 10),
            state="disabled",
            spacing1=2,
            spacing3=4,
            # Tk denies keyboard focus to a disabled widget unless asked, and
            # without focus Ctrl+C never reaches the transcript.
            takefocus=True,
            cursor="xterm",
            selectbackground=COLORS["select"],
            selectforeground=COLORS["text"],
            # Keep the selection visible after focus moves to the input box,
            # otherwise the highlight vanishes before you can copy it.
            inactiveselectbackground=COLORS["select_idle"],
        )
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self._vsb = vsb
        self.text.configure(yscrollcommand=self._on_scroll)
        self.text.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        vsb.grid(row=0, column=1, sticky="ns", pady=1, padx=(0, 1))
        self._build_outline()
        self._build_info()
        self._configure_tags()

        # Shown only while you are scrolled away from the tail.
        self.jump_button = tk.Button(
            body, text="↓ 跳到最新", bd=0, padx=10, pady=4, cursor="hand2",
            bg=COLORS["accent"], fg="white", activebackground=COLORS["user"],
            activeforeground="white", font=(app.ui_font, 9),
            command=self.jump_to_latest,
        )

        # Strip of attached images, sitting just above the input box and shown
        # only when something is attached.
        self.attach_bar = ttk.Frame(self, style="TFrame")
        self.attach_bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.attach_bar.grid_remove()

        composer = ttk.Frame(self, style="TFrame")
        composer.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        composer.columnconfigure(0, weight=1)

        input_wrap = tk.Frame(composer, bg=COLORS["border"])
        input_wrap.grid(row=0, column=0, sticky="ew")
        self.input = tk.Text(
            input_wrap,
            height=4,
            wrap="word",
            bd=0,
            padx=10,
            pady=8,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(app.ui_font, 10),
            undo=True,
        )
        self.input.pack(fill="both", expand=True, padx=1, pady=1)
        self.input.bind("<Return>", self._on_send_key)
        self.input.bind("<Shift-Return>", lambda e: None)  # let Text insert a newline
        self.input.bind("<KeyRelease>", lambda e: self._refresh_send_state())

        side = ttk.Frame(composer, style="TFrame")
        side.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.send_button = ttk.Button(side, text="送出\nEnter", command=self.on_send, width=12)
        self.send_button.pack(fill="x")
        self.stop_button = ttk.Button(side, text="停止", command=self.on_stop, width=12, state="disabled")
        self.stop_button.pack(fill="x", pady=(6, 0))
        self.attach_button = ttk.Button(side, text="附加圖片", style="Toolbar.TButton",
                                        command=self.attach_files, width=12)
        self.attach_button.pack(fill="x", pady=(6, 0))
        _Tooltip(self.attach_button, "附加圖片給 Codex（也可以直接在輸入框按 Ctrl+V 貼上截圖）")

        self._bind_transcript()  # needs self.input, so it runs last
        self._refresh_send_state()

    # ------------------------------------------------------- side panels

    def _build_outline(self) -> None:
        """The navigation rail: your questions and Codex's headings."""
        self.outline = tk.Frame(self.splitter, bg=COLORS["bg"],
                                width=self.app.outline_width)
        # The children are packed, so pack_propagate is what stops them forcing
        # the rail wider than the sash puts it.
        self.outline.pack_propagate(False)
        ttk.Label(self.outline, text="大綱", style="Section.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        self.outline_body = tk.Frame(self.outline, bg=COLORS["bg"])
        self.outline_body.pack(fill="both", expand=True)
        self._outline_targets: list[str] = []

    def _build_info(self) -> None:
        """The details panel: settings, usage, attachments, changed files."""
        self.info = tk.Frame(self.splitter, bg=COLORS["bg"],
                             width=self.app.info_width)
        self.info.pack_propagate(False)
        ttk.Label(self.info, text="資訊", style="Section.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        self.info_body = tk.Frame(self.info, bg=COLORS["bg"])
        self.info_body.pack(fill="both", expand=True)
        self._info_thumbs: list[tk.PhotoImage] = []

    def _decide_panels(self, width: int) -> tuple[bool, bool]:
        """Which rails fit, given the width. Preference first, then room.

        A narrow window cannot afford both rails and a readable column, so the
        rails give way rather than squeezing the transcript to nothing. The
        details panel goes first because the outline is the navigation aid.
        """
        outline, info = self.app.show_outline, self.app.show_info
        if width <= 1:
            return outline, info          # not laid out yet; decide again on resize
        # Costs are the widths you dragged to, not fixed constants.
        outline_cost = self.app.outline_width + 6
        info_cost = self.app.info_width + 6
        room = width - (outline_cost if outline else 0) - (info_cost if info else 0)
        if room < MIN_CONTENT_PX and info:
            info, room = False, room + info_cost
        if room < MIN_CONTENT_PX and outline:
            outline = False
        return outline, info

    def apply_panels(self) -> None:
        """Re-decide the rails for the current width and rebuild the splitter."""
        self._panels_shown = None         # force the comparison below to differ
        self._fit_content_width(None)

    def _rebuild_splitter(self, outline: bool, info: bool) -> None:
        """Set the splitter's panes at the widths you dragged them to.

        The width comes from each rail's requested size, which is what
        PanedWindow honours when a pane is added. Calling `sashpos` right after
        `add` does not stick — the panes have no geometry yet at that point.
        """
        for pane in self.splitter.panes():
            self.splitter.forget(pane)
        self.outline.configure(width=self.app.outline_width)
        self.info.configure(width=self.app.info_width)
        if outline:
            self.splitter.add(self.outline, weight=0)
        self.splitter.add(self.body, weight=1)
        if info:
            self.splitter.add(self.info, weight=0)

    def _remember_rail_widths(self, _event=None) -> None:
        """After a drag, store the rail widths so they survive a restart."""
        if not self._panels_shown:
            return
        outline, info = self._panels_shown
        changed = False
        if outline and self.outline.winfo_width() > 1:
            width = self.outline.winfo_width()
            if width != self.app.outline_width:
                self.app.outline_width, changed = width, True
        if info and self.info.winfo_width() > 1:
            width = self.info.winfo_width()
            if width != self.app.info_width:
                self.app.info_width, changed = width, True
        if changed:
            ui = self.app.ws.data.setdefault("ui", {})
            ui["outline_width"] = self.app.outline_width
            ui["info_width"] = self.app.info_width
            self.app.ws.touch()
            self.refresh_info()          # its wraplength depends on the width

    # ------------------------------------------------- width and scrolling

    def _fit_content_width(self, event=None) -> None:
        """Show/hide the rails, then cap the transcript's measure if asked."""
        width = (event.width if event is not None else self.winfo_width()) or 0
        decision = self._decide_panels(width)
        if decision != self._panels_shown:
            outline, info = decision
            self._panels_shown = decision
            self._rebuild_splitter(outline, info)
            if outline:
                self.refresh_outline()
            if info:
                self.refresh_info()

    def _fit_text_measure(self, event=None) -> None:
        """Keep the text column to a readable measure inside its pane."""
        width = (event.width if event is not None else self.body.winfo_width()) or 0
        pad = max(0, width - MAX_CONTENT_PX) if self.app.limit_width else 0
        if pad != self._text_pad:
            self._text_pad = pad
            # The widget keeps filling its pane; only the text is inset. Padding
            # *around* the widget instead left the visible edge of the white area
            # ~180px away from the sash, so there was nothing to grab there.
            self.text.configure(padx=TEXT_INSET_PX + pad // 2)

    def following_tail(self) -> bool:
        """True when the transcript's last line is actually on screen.

        Asking the widget whether it is displaying that line is exact. A
        `yview()` fraction is not: after a relayout it does not reliably reach
        1.0, so any threshold was either too tight (a jump button appearing
        while you sit at the bottom) or too loose to mean anything.
        """
        try:
            if not self.text.winfo_ismapped():
                return True          # nothing laid out yet; do not offer a jump
            return self.text.dlineinfo("end-1c") is not None
        except tk.TclError:
            return True

    def _on_scroll(self, first: str, last: str) -> None:
        self._vsb.set(first, last)
        self._update_jump_button()

    def _update_jump_button(self) -> None:
        if self.following_tail():
            self.jump_button.place_forget()
        else:
            self.jump_button.place(relx=1.0, rely=1.0, x=-24, y=-12, anchor="se")

    def jump_to_latest(self) -> None:
        self.text.see("end")
        self.text.yview_moveto(1.0)
        self._update_jump_button()

    def outline_entries(self) -> list[tuple[str, int, str]]:
        """(label, depth, index) built from tags already in the transcript.

        Reading the widget avoids threading positions through the renderer: the
        role labels and Markdown headings are tagged, so their ranges are the
        outline.
        """
        found: list[tuple[tuple[int, int], str, int, str]] = []

        def key(index: str) -> tuple[int, int]:
            line, _, char = index.partition(".")
            return int(line), int(char)

        ranges = self.text.tag_ranges("role_user")
        for i in range(0, len(ranges), 2):
            end = str(ranges[i + 1])
            label = self.text.get(end, f"{end} lineend").strip()
            found.append((key(end), label or "（訊息）", 0, end))

        for level, tag in ((1, "md_h1"), (2, "md_h2"), (3, "md_h3")):
            ranges = self.text.tag_ranges(tag)
            for i in range(0, len(ranges), 2):
                start, end = str(ranges[i]), str(ranges[i + 1])
                label = self.text.get(start, end).strip()
                if label:
                    found.append((key(start), label, level, start))

        found.sort(key=lambda item: item[0])
        return [(label, depth, index) for _, label, depth, index in found]

    def refresh_outline(self) -> None:
        for child in self.outline_body.winfo_children():
            child.destroy()
        self._outline_targets = []
        entries = self.outline_entries()
        if not entries:
            ttk.Label(self.outline_body, text="（還沒有內容）", style="Muted.TLabel").pack(
                anchor="w"
            )
            return
        for label, depth, index in entries:
            trimmed = label if len(label) <= 24 else label[:23] + "…"
            row = tk.Label(
                self.outline_body,
                text=("你  " if depth == 0 else "　" * depth) + trimmed,
                bg=COLORS["bg"],
                fg=COLORS["user"] if depth == 0 else COLORS["muted"],
                font=(self.app.ui_font, 9, "bold" if depth == 0 else "normal"),
                anchor="w", justify="left", cursor="hand2", padx=2,
            )
            row.pack(fill="x")
            row.bind("<Button-1>", lambda e, i=index: self.jump_to(i))
            row.bind("<Enter>", lambda e, w=row: w.configure(bg=COLORS["user_bg"]))
            row.bind("<Leave>", lambda e, w=row: w.configure(bg=COLORS["bg"]))
            self._outline_targets.append(index)

    def jump_to(self, index: str) -> None:
        """Put a transcript position at the top of the view."""
        self.text.see(index)
        self.text.yview(index)
        self._update_jump_button()

    def refresh_info(self) -> None:
        for child in self.info_body.winfo_children():
            child.destroy()
        self._info_thumbs = []
        conv = self.app.ws.find(self.conv_id)
        if conv is None:
            return

        settings = self.app.ws.resolve(conv["id"])
        usage = self.app.ws.usage_of(conv["id"])
        agent_id = self.app.ws.conversation_agent(conv["id"])
        session = (conv.get("claude_session_id") if agent_id == store.CLAUDE_AGENT
                   else conv.get("thread_id")) or "尚未建立"
        rows = [
            ("Agent", AGENT_LABELS[agent_id]),
            ("工作目錄", settings["cwd"]),
            ("模型", settings.get("model") or "(config 預設)"),
            ("沙箱", str(settings.get("sandbox"))),
            ("session", session),
        ]
        if conv.get("fork_of_name"):
            rows.append(("分岔自", conv["fork_of_name"]))
        if usage.get("turns"):
            rows.append(("用量", "{} 回 · in {:,} · out {:,}".format(
                usage.get("turns", 0), usage.get("input_tokens", 0),
                usage.get("output_tokens", 0))))
        instructions = self.app.ws.instructions_for(conv["id"])
        if instructions:
            summary = instructions.replace(chr(10), " ")
            if len(summary) > 160:
                summary = summary[:160] + "…"
            rows.append((f"專案提示詞（{len(instructions)} 字）", summary))

        for name, value in rows:
            ttk.Label(self.info_body, text=name, style="Muted.TLabel").pack(anchor="w")
            tk.Label(self.info_body, text=value, bg=COLORS["bg"], fg=COLORS["text"],
                     font=(self.app.mono_font, 8), anchor="w", justify="left",
                     wraplength=INFO_WIDTH - 16).pack(anchor="w", pady=(0, 6))

        images = [p for m in conv["messages"] for p in (m.get("images") or ())]
        if images:
            ttk.Label(self.info_body, text=f"附件（{len(images)}）",
                      style="Muted.TLabel").pack(anchor="w", pady=(4, 2))
            strip = tk.Frame(self.info_body, bg=COLORS["bg"])
            strip.pack(anchor="w")
            for path in images[:6]:
                thumb = self._thumbnail(path)
                if thumb is None:
                    continue
                self._info_thumbs.append(thumb)
                holder = tk.Label(strip, image=thumb, bg=COLORS["bg"], cursor="hand2")
                holder.pack(side="left", padx=(0, 4))
                holder.bind("<Button-1>", lambda e, p=path: self._open_path(p))

        changed = [m["text"] for m in conv["messages"]
                   if m["role"] == "tool" and m["text"].startswith("檔案變更")]
        if changed:
            ttk.Label(self.info_body, text="Codex 動過的檔案",
                      style="Muted.TLabel").pack(anchor="w", pady=(6, 2))
            seen: list[str] = []
            for block in changed:
                for line in block.split("\n")[1:]:
                    entry = line.strip()
                    if entry and entry not in seen:
                        seen.append(entry)
            for entry in seen[:12]:
                tk.Label(self.info_body, text=entry, bg=COLORS["bg"], fg=COLORS["tool"],
                         font=(self.app.mono_font, 8), anchor="w", justify="left",
                         wraplength=INFO_WIDTH - 16).pack(anchor="w")

        tool_events = [m["text"] for m in conv["messages"] if m["role"] == "agent_tool"]
        if tool_events:
            ttk.Label(self.info_body, text=f"工具紀錄（{len(tool_events)}）",
                      style="Muted.TLabel").pack(anchor="w", pady=(10, 2))
            holder = tk.Frame(self.info_body, bg=COLORS["border"])
            holder.pack(fill="x")
            holder.columnconfigure(0, weight=1)
            log = tk.Text(
                holder, height=min(9, max(3, len(tool_events))), wrap="word", bd=0,
                padx=6, pady=5, bg=COLORS["panel"], fg=COLORS["tool"],
                font=(self.app.mono_font, 8), state="normal", cursor="arrow",
            )
            bar = ttk.Scrollbar(holder, orient="vertical", command=log.yview)
            log.configure(yscrollcommand=bar.set)
            log.grid(row=0, column=0, sticky="ew", padx=(1, 0), pady=1)
            bar.grid(row=0, column=1, sticky="ns", pady=1, padx=(0, 1))
            for index, event in enumerate(tool_events, start=1):
                log.insert("end", f"{index}. {event}\n")
            log.configure(state="disabled")

    def _bind_transcript(self) -> None:
        """Make the read-only transcript behave like selectable, copyable text."""
        # Clicking must hand over focus, or the copy keys go nowhere.
        self.text.bind("<Button-1>", lambda e: self.text.focus_set(), add="+")
        for sequence in ("<Control-c>", "<Control-C>", "<Control-Insert>"):
            self.text.bind(sequence, self.copy_selection)
        for sequence in ("<Control-a>", "<Control-A>"):
            self.text.bind(sequence, self.select_all)
        # Ctrl+Enter should still send, even when the transcript holds focus.
        self.text.bind("<Control-Return>", self._on_send_key)
        self.text.bind("<Button-3>", self._transcript_menu)
        self.input.bind("<Button-3>", self._input_menu)
        # Ctrl+V attaches a screenshot when the clipboard holds one, and
        # otherwise falls through to Tk's ordinary text paste.
        self.input.bind("<Control-v>", self._on_paste)
        self.input.bind("<Control-V>", self._on_paste)

    # ---------------------------------------------------------- attachments

    def _attachment_dir(self) -> str:
        return os.path.join(self.app.ws.home, "attachments")

    def current_attachments(self) -> list[str]:
        return self.attachments.setdefault(self.conv_id, []) if self.conv_id else []

    def _on_paste(self, _event=None):
        """Ctrl+V: attach an image if there is one, otherwise paste text."""
        if not self.conv_id:
            return None
        if not clipboard_image.has_image() and not clipboard_image.clipboard_files():
            return None  # nothing image-like: let Tk paste text as usual
        return "break" if self.paste_image() else None

    def attach_files(self) -> None:
        if not self.conv_id:
            return
        chosen = filedialog.askopenfilenames(
            parent=self,
            title="附加圖片",
            filetypes=[("圖片", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("所有檔案", "*.*")],
        )
        if chosen:
            self.add_attachments(list(chosen))

    def add_attachments(self, paths: list[str]) -> None:
        if not self.conv_id:
            return
        current = self.current_attachments()
        added = 0
        for path in paths:
            if os.path.isfile(path) and path not in current:
                current.append(path)
                added += 1
        if added:
            self.refresh_attachments()
            self.app.set_status(f"已附加 {added} 個圖片檔，送出時一併傳給 Codex")

    def remove_attachment(self, path: str) -> None:
        current = self.current_attachments()
        if path in current:
            current.remove(path)
            self.refresh_attachments()

    def _thumbnail(self, path: str) -> tk.PhotoImage | None:
        """A small preview, for the formats Tk can decode itself (PNG / GIF)."""
        try:
            image = tk.PhotoImage(master=self, file=path)
        except tk.TclError:
            return None  # JPEG/WEBP: Tk cannot read these, show the name only
        factor = max(1, -(-image.height() // THUMBNAIL_HEIGHT))
        return image.subsample(factor, factor) if factor > 1 else image

    def refresh_attachments(self) -> None:
        for child in self.attach_bar.winfo_children():
            child.destroy()
        # Tk does not keep its own reference to a PhotoImage, so dropping ours
        # would blank every thumbnail the moment this method returns.
        self._thumbnails: list[tk.PhotoImage] = []

        current = self.current_attachments()
        if not current:
            self.attach_bar.grid_remove()
            self._refresh_send_state()
            return

        for path in current:
            card = tk.Frame(self.attach_bar, bg=COLORS["border"])
            card.pack(side="left", padx=(0, 6))
            inner = tk.Frame(card, bg=COLORS["panel"])
            inner.pack(padx=1, pady=1)

            thumb = self._thumbnail(path)
            if thumb is not None:
                self._thumbnails.append(thumb)
                tk.Label(inner, image=thumb, bg=COLORS["panel"], bd=0).pack(
                    side="left", padx=(3, 5), pady=3
                )
            else:
                tk.Label(inner, text="🖼", bg=COLORS["panel"],
                         font=(self.app.ui_font, 14)).pack(side="left", padx=(6, 5), pady=3)

            name = os.path.basename(path)
            if len(name) > 26:
                name = name[:12] + "…" + name[-10:]
            tk.Label(inner, text=name, bg=COLORS["panel"], fg=COLORS["muted"],
                     font=(self.app.ui_font, 8)).pack(side="left", padx=(0, 2))
            tk.Button(inner, text="✕", bd=0, bg=COLORS["panel"], fg=COLORS["muted"],
                      font=(self.app.ui_font, 8), activebackground=COLORS["user_bg"],
                      cursor="hand2", padx=4,
                      command=lambda p=path: self.remove_attachment(p)).pack(
                side="left", padx=(0, 2)
            )
        self.attach_bar.grid()
        self._refresh_send_state()

    def _transcript_menu(self, event) -> None:
        self.text.focus_set()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label="複製  (Ctrl+C)",
            command=self.copy_selection,
            state="normal" if self.selection() else "disabled",
        )
        menu.add_command(label="全選  (Ctrl+A)", command=self.select_all)
        menu.add_separator()
        menu.add_command(label="複製整段對話", command=self.app.copy_transcript)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _input_menu(self, event) -> None:
        self.input.focus_set()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="剪下", command=lambda: self.input.event_generate("<<Cut>>"))
        menu.add_command(label="複製", command=lambda: self.input.event_generate("<<Copy>>"))

        label, is_image = self.paste_entry()
        if is_image:
            menu.add_command(label=label, command=self.paste_image)
        else:
            menu.add_command(label=label,
                             command=lambda: self.input.event_generate("<<Paste>>"))

        menu.add_separator()
        menu.add_command(label="附加圖片…", command=self.attach_files)
        menu.add_command(
            label="全選",
            command=lambda: self.input.tag_add("sel", "1.0", "end-1c"),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def paste_entry(self) -> tuple[str, bool]:
        """Label for the paste menu item, and whether it pastes an image.

        Right-click paste follows whatever the clipboard actually holds: a
        screenshot becomes an attachment, anything else pastes as text.
        """
        if clipboard_image.has_image():
            return "貼上圖片", True
        files = clipboard_image.clipboard_files()
        if files:
            return f"貼上圖片（{len(files)} 個檔案）", True
        return "貼上", False

    def paste_image(self) -> bool:
        """Attach whatever image the clipboard holds. True if anything landed."""
        if not self.conv_id:
            return False
        pasted: list[str] = []
        if clipboard_image.has_image():
            saved = clipboard_image.save_clipboard_image(self._attachment_dir())
            if saved:
                pasted.append(saved)
        if not pasted:
            pasted = clipboard_image.clipboard_files()
        if not pasted:
            self.app.set_status("剪貼簿裡沒有圖片")
            return False
        self.add_attachments(pasted)
        return True

    def selection(self) -> str:
        try:
            return self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def copy_selection(self, _event=None):
        selected = self.selection()
        if selected:
            self.clipboard_clear()
            self.clipboard_append(selected)
            self.app.set_status(f"已複製 {len(selected)} 個字元")
        else:
            self.app.set_status("沒有選取任何文字")
        return "break"

    def select_all(self, _event=None):
        self.text.tag_remove("sel", "1.0", "end")
        self.text.tag_add("sel", "1.0", "end-1c")
        self.text.focus_set()
        return "break"

    def _on_header_resize(self, event) -> None:
        available = max(160, event.width - self._header_buttons.winfo_reqwidth() - 20)
        if available != self._wrap:
            self._wrap = available
            self.title_label.configure(wraplength=available)
            self.meta_label.configure(wraplength=available)
            # The warning spans both columns, so it may use the full width.
            self.warn_label.configure(wraplength=max(200, event.width - 24))

    def _configure_tags(self) -> None:
        app = self.app
        self.text.tag_configure(
            "role_user", foreground=COLORS["user"], font=(app.ui_font, 10, "bold"),
            spacing1=12, justify="right", rmargin=14,
        )
        self.text.tag_configure(
            "role_agent", foreground=COLORS["agent"], font=(app.ui_font, 10, "bold"), spacing1=10
        )
        self.text.tag_configure(
            "role_other", foreground=COLORS["muted"], font=(app.ui_font, 9, "bold"), spacing1=8
        )
        # Your own messages sit on the right, Codex's on the left. No background
        # tint: Tk paints a tag's background across the entire line rather than
        # behind the glyphs, so it reads as a full-width band, not a bubble.
        self.text.tag_configure(
            "user", foreground=COLORS["user"], justify="right",
            lmargin1=90, lmargin2=90, rmargin=14, spacing1=1, spacing3=3,
        )
        # A single tinted space on its own line, at a 1pt font: a 3px rule.
        self.text.tag_configure(
            "separator", background=COLORS["border"], font=(app.ui_font, 1),
            spacing1=9, spacing3=9,
        )
        self.text.tag_configure("agent", foreground=COLORS["agent"], lmargin1=12, lmargin2=12)
        self.text.tag_configure(
            "reasoning", foreground=COLORS["reasoning"], font=(app.ui_font, 9, "italic"),
            lmargin1=12, lmargin2=12,
        )
        self.text.tag_configure(
            "tool", foreground=COLORS["tool"], font=(app.mono_font, 9), background=COLORS["tool_bg"],
            lmargin1=12, lmargin2=12,
        )
        self.text.tag_configure("error", foreground=COLORS["error"], lmargin1=12, lmargin2=12)
        self.text.tag_configure("notice", foreground=COLORS["notice"], font=(app.ui_font, 9),
                                lmargin1=12, lmargin2=12)
        self.text.tag_configure("meta", foreground=COLORS["muted"], font=(app.ui_font, 9), lmargin1=12)
        self.text.tag_configure("log", foreground=COLORS["log"], font=(app.mono_font, 8), lmargin1=12)
        # Created last so Markdown styling outranks the per-role body tags.
        self.text.tag_configure("tool_hint", foreground=COLORS["muted"],
                                font=(app.mono_font, 8))
        richtext.configure_tags(self.text, app.ui_font, app.mono_font, COLORS)

    # ------------------------------------------------------------ display

    def show(self, conv: dict[str, Any]) -> None:
        if self.conv_id and self.conv_id != conv["id"]:
            self._stash_draft()
        # Embedded previews are re-created from scratch for the new transcript.
        self._inline_images: list[tk.PhotoImage] = []
        self._link_targets: dict[str, str] = {}
        self._tool_blocks: list[tuple[str, str, str]] = []
        self.conv_id = conv["id"]
        self.input.delete("1.0", "end")
        draft = self.drafts.get(conv["id"])
        if draft:
            self.input.insert("1.0", draft)
        self.refresh_attachments()
        self.refresh_header()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for message in conv["messages"]:
            self._write(message["role"], message["text"], message.get("images"),
                        message.get("agent_id"))
        self.text.configure(state="disabled")
        self.text.see("end")
        self.apply_panels()
        # Showing the rails re-wraps the transcript, which shifts the view, so
        # settle at the tail once the layout has finished changing.
        self.cancel_pending()
        self._settle_job = self.after_idle(self._settle_at_tail)
        self.set_running(conv["id"] in self.app.turns)

    def cancel_pending(self) -> None:
        """Drop any deferred work, so nothing fires after the window closes."""
        job, self._settle_job = getattr(self, "_settle_job", None), None
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass

    def _settle_at_tail(self) -> None:
        self._settle_job = None
        try:
            self.text.see("end")
            self.text.yview_moveto(1.0)
            self._update_jump_button()
        except tk.TclError:
            pass  # the view went away before the idle callback ran

    def refresh_header(self) -> None:
        conv = self.app.ws.find(self.conv_id)
        if conv is None:
            return
        self.title_label.configure(text=self.app.ws.path_of(conv["id"]))
        settings = self.app.ws.resolve(conv["id"])
        agent_id = self.app.ws.conversation_agent(conv["id"])
        self.agent_var.set(AGENT_LABELS[agent_id])
        if agent_id == store.CLAUDE_AGENT:
            thread = conv.get("claude_session_id") or "尚未建立"
        elif conv.get("thread_id"):
            thread = conv["thread_id"]
        elif conv.get("fork_of"):
            thread = f"送出後才建立（將分岔自 {conv['fork_of']}）"
        else:
            thread = "尚未建立"
        meta = "Agent: {}    cwd: {}    模型: {}    sandbox: {}    session: {}".format(
            AGENT_LABELS[agent_id],
            settings["cwd"], settings.get("model") or "(config 預設)",
            settings.get("sandbox"), thread,
        )
        if conv.get("fork_of_name"):
            meta += f"\n分岔自「{conv['fork_of_name']}」"
        self.meta_label.configure(text=meta)
        # Nothing to branch from until this conversation has a thread of its own.
        self.fork_button.configure(state="normal" if agent_id == store.CODEX_AGENT and conv.get("thread_id") else "disabled")
        warning = codex_runner.sandbox_warning(settings["cwd"], settings.get("sandbox")) if agent_id == store.CODEX_AGENT else None
        if warning:
            self.warn_label.configure(text="⚠  " + warning)
            self.warn_label.grid()
        else:
            self.warn_label.grid_remove()

    def _on_agent_changed(self, _event=None) -> None:
        if not self.conv_id:
            return
        agent_id = next((key for key, label in AGENT_LABELS.items() if label == self.agent_var.get()), store.CODEX_AGENT)
        if self.conv_id in self.app.turns:
            self.refresh_header()
            self.app.set_status("Agent 執行中，完成後才能切換。")
            return
        self.app.ws.set_conversation_agent(self.conv_id, agent_id)
        self.refresh_header()
        self.app.set_status(f"此對話已改用 {AGENT_LABELS[agent_id]}")

    def append(self, role: str, text: str, images: list[str] | None = None,
               agent_id: str | None = None) -> None:
        # Decide before inserting: afterwards the view is no longer at the tail.
        follow = self.following_tail()
        self.text.configure(state="normal")
        self._write(role, text, images, agent_id)
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")
        self._update_jump_button()
        if self._panels_shown and self._panels_shown[0]:
            self.refresh_outline()

    def append_log(self, text: str) -> None:
        follow = self.following_tail()
        self.text.configure(state="normal")
        self.text.insert("end", codex_runner.clean_output(text).rstrip() + "\n", "log")
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")
        self._update_jump_button()

    def _write(self, role: str, text: str, images: list[str] | None = None,
               agent_id: str | None = None) -> None:
        # Also applied here, not just on ingest, so transcripts recorded before
        # the escape stripping existed still render cleanly.
        text = codex_runner.clean_output(text)
        if images:
            # Older transcripts baked the file names into the message text;
            # they are drawn from `images` now, so drop the duplicated lines.
            text = _ATTACHMENT_LINE.sub("", text).rstrip()
        if self.text.index("end-1c") != "1.0":
            self.text.insert("end", " \n", "separator")
        role_tag = {"user": "role_user", "agent": "role_agent"}.get(role, "role_other")
        body_tag = (
            role
            if role in ("user", "agent", "reasoning", "tool", "error", "notice", "meta")
            else "agent"
        )
        if role != "meta":
            label = (AGENT_LABELS.get(agent_id or self.app.ws.conversation_agent(self.conv_id or ""))
                     if role == "agent" else ROLE_LABELS.get(role, role))
            self.text.insert("end", label + "\n", role_tag)
        if role == "agent":
            # Only the agent's prose is Markdown; command output and the user's
            # own text are shown exactly as written.
            richtext.insert(self.text, text.strip(), (body_tag,))
        elif role == "tool":
            self._write_tool(text, body_tag)
        elif text:
            self.text.insert("end", text.rstrip() + "\n", body_tag)
        for path in images or ():
            self._write_attachment(path, body_tag)

    def _write_tool(self, text: str, body_tag: str) -> None:
        """Draw a command execution, collapsed to one clickable line by default.

        The output is inserted either way and hidden with the tag's `elide`
        option, so expanding is instant and does not disturb the scroll position
        — and Ctrl+A still copies the full output even while it is hidden.
        """
        mode = self.app.tool_display
        if mode == TOOL_HIDDEN:
            return
        text = text.rstrip()
        if mode == TOOL_FULL or "\n" not in text:
            self.text.insert("end", text + "\n", body_tag)
            return

        head, _, rest = text.partition("\n")
        summary = head if len(head) <= TOOL_SUMMARY_CHARS else head[:TOOL_SUMMARY_CHARS] + "…"
        key = len(self._tool_blocks)
        closed, opened = f"toolarrowc{key}", f"toolarrowo{key}"
        head_tag, body_id = f"toolhead{key}", f"toolbody{key}"

        # Both arrows are inserted once; toggling only flips which one is elided,
        # so the widget never has to be edited again.
        self.text.insert("end", "▸ ", (body_tag, closed, head_tag))
        self.text.insert("end", "▾ ", (body_tag, opened, head_tag))
        self.text.insert("end", summary, (body_tag, head_tag))
        self.text.insert("end", f"    （{rest.count(chr(10)) + 1} 行輸出）\n",
                         (body_tag, head_tag, "tool_hint"))
        self.text.insert("end", rest + "\n", (body_tag, body_id))

        self.text.tag_configure(opened, elide=True)
        self.text.tag_configure(body_id, elide=True)
        self.text.tag_configure(head_tag, foreground=COLORS["accent"])
        self.text.tag_bind(head_tag, "<Button-1>", lambda e, k=key: self._toggle_tool(k))
        self.text.tag_bind(head_tag, "<Enter>", lambda e: self.text.configure(cursor="hand2"))
        self.text.tag_bind(head_tag, "<Leave>", lambda e: self.text.configure(cursor="xterm"))
        self._tool_blocks.append((closed, opened, body_id))

    def _toggle_tool(self, key: int) -> None:
        try:
            closed, opened, body_id = self._tool_blocks[key]
        except IndexError:
            return
        hidden = str(self.text.tag_cget(body_id, "elide")) in ("1", "true", "True")
        self.text.tag_configure(body_id, elide=not hidden)
        self.text.tag_configure(closed, elide=hidden)
        self.text.tag_configure(opened, elide=not hidden)

    def _write_attachment(self, path: str, body_tag: str) -> None:
        """Draw one attachment: a clickable preview plus its clickable name."""
        tag = f"imgopen{len(self._link_targets)}"
        self._link_targets[tag] = path
        self.text.tag_configure(tag, foreground=COLORS["accent"], underline=True)
        self.text.tag_bind(tag, "<Button-1>", lambda e, t=tag: self._open_link(t))
        self.text.tag_bind(tag, "<Enter>", lambda e: self.text.configure(cursor="hand2"))
        self.text.tag_bind(tag, "<Leave>", lambda e: self.text.configure(cursor="xterm"))

        thumb = self._thumbnail(path)
        if thumb is not None:
            self._inline_images.append(thumb)
            start = self.text.index("end-1c")
            self.text.image_create("end", image=thumb, padx=4, pady=3)
            # An embedded image occupies one index, so tag it after the fact.
            self.text.tag_add(tag, start, f"{start}+1c")
            self.text.tag_add(body_tag, start, f"{start}+1c")
            self.text.insert("end", "\n", body_tag)

        self.text.insert("end", os.path.basename(path), (body_tag, tag))
        self.text.insert("end", "\n", body_tag)

    def _open_link(self, tag: str) -> None:
        path = self._link_targets.get(tag)
        if path:
            self._open_path(path)

    def _open_path(self, path: str) -> None:
        if not os.path.isfile(path):
            self.app.set_status(f"檔案已不存在：{path}")
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - user clicked it
            else:
                subprocess.Popen(["xdg-open", path])
            self.app.set_status(f"已開啟 {os.path.basename(path)}")
        except OSError as exc:
            self.app.set_status(f"無法開啟：{exc}")

    def _stash_draft(self) -> None:
        text = self.input.get("1.0", "end").strip()
        if text:
            self.drafts[self.conv_id] = text
        else:
            self.drafts.pop(self.conv_id, None)

    def refresh_progress(self) -> None:
        """Show elapsed time and what the running turn is currently doing."""
        text = self.app.running_progress(self.conv_id) if self.conv_id else ""
        if text:
            self.run_label.configure(text=text)
            self.run_label.grid()
        else:
            self.run_label.grid_remove()

    def set_running(self, running: bool) -> None:
        self.stop_button.configure(state="normal" if running else "disabled")
        self.refresh_progress()
        self._refresh_send_state()

    def _refresh_send_state(self) -> None:
        """Enable Send only when the composer has text or an attachment."""
        has_payload = bool(self.input.get("1.0", "end").strip()) or bool(self.current_attachments())
        self.send_button.configure(state="normal" if has_payload else "disabled")

    def queued_count(self, conv_id: str | None = None) -> int:
        return len(self.queued.get(conv_id or self.conv_id or "", ()))

    def drop_queue(self, conv_id: str) -> None:
        self.queued.pop(conv_id, None)

    def start_next_queued(self, conv_id: str) -> bool:
        """Start the next queued prompt after the active subprocess exits."""
        pending = self.queued.get(conv_id)
        if not pending or conv_id in self.app.turns:
            return False
        prompt, images = pending.pop(0)
        if not pending:
            self.queued.pop(conv_id, None)
        self.app.send(conv_id, prompt, images=images)
        if conv_id == self.conv_id:
            self._refresh_send_state()
        return True

    def focus_input(self) -> None:
        self.input.focus_set()

    def open_cwd(self) -> None:
        if self.conv_id:
            _open_in_explorer(self.app.ws.resolve(self.conv_id)["cwd"])

    # ------------------------------------------------------------ actions

    def _on_send_key(self, _event):
        self.on_send()
        return "break"

    def on_send(self) -> None:
        if not self.conv_id:
            return
        prompt = self.input.get("1.0", "end").strip()
        images = list(self.current_attachments())
        if not prompt and not images:
            return
        if not prompt:
            prompt = IMAGE_ONLY_PROMPT
        if images and self.app.ws.conversation_agent(self.conv_id) == store.CLAUDE_AGENT:
            self.app.set_status("Claude Code 的第一版尚不支援圖片附件，請移除附件後再送出。")
            return
        if self.conv_id in self.app.turns:
            self.queued.setdefault(self.conv_id, []).append((prompt, images))
            self.input.delete("1.0", "end")
            self.drafts.pop(self.conv_id, None)
            self.attachments.pop(self.conv_id, None)
            self.refresh_attachments()
            self.app.set_status(f"已加入佇列（還有 {self.queued_count()} 則會在目前回合完成後自動送出）")
            return
        if prompt.startswith("/"):
            self.input.delete("1.0", "end")
            self.drafts.pop(self.conv_id, None)
            self.app.handle_slash_command(self.conv_id, prompt)
            return
        self.input.delete("1.0", "end")
        self.drafts.pop(self.conv_id, None)
        self.attachments.pop(self.conv_id, None)
        self.refresh_attachments()
        self.app.send(self.conv_id, prompt, images=images)

    def on_stop(self) -> None:
        if self.conv_id:
            self.app.stop_turn(self.conv_id)


class ProjectView(ttk.Frame):
    """Per-project settings. Blank fields mean "inherit from the parent"."""

    def __init__(self, master: tk.Widget, app: TreeAgentApp) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self.project_id: str | None = None
        self.columnconfigure(0, weight=1)

        self.title_label = ttk.Label(self, text="", style="Title.TLabel", anchor="w")
        self.title_label.grid(row=0, column=0, sticky="ew")
        self.summary_label = ttk.Label(self, text="", style="Muted.TLabel", anchor="w")
        self.summary_label.grid(row=1, column=0, sticky="ew", pady=(2, 14))

        form = ttk.Frame(self, style="TFrame")
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="工作目錄", style="TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.cwd_var = tk.StringVar()
        cwd_row = ttk.Frame(form, style="TFrame")
        cwd_row.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        cwd_row.columnconfigure(0, weight=1)
        ttk.Entry(cwd_row, textvariable=self.cwd_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(cwd_row, text="瀏覽…", style="Toolbar.TButton",
                   command=self.browse_cwd).grid(row=0, column=1, padx=(6, 0))
        self.cwd_hint = ttk.Label(form, text="", style="Muted.TLabel")
        self.cwd_hint.grid(row=1, column=1, sticky="w", padx=(10, 0))

        ttk.Label(form, text="模型", style="TLabel").grid(row=2, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.model_var).grid(row=2, column=1, sticky="ew", padx=(10, 0))
        self.model_hint = ttk.Label(form, text="", style="Muted.TLabel")
        self.model_hint.grid(row=3, column=1, sticky="w", padx=(10, 0))

        ttk.Label(form, text="沙箱模式", style="TLabel").grid(row=4, column=0, sticky="w", pady=4)
        self.sandbox_var = tk.StringVar()
        ttk.Combobox(
            form,
            textvariable=self.sandbox_var,
            values=(INHERIT,)
            + tuple(codex_runner.sandbox_label(m) for m in codex_runner.SANDBOX_MODES),
            state="readonly",
            width=46,
        ).grid(row=4, column=1, sticky="w", padx=(10, 0))
        self.sandbox_hint = ttk.Label(form, text="", style="Muted.TLabel")
        self.sandbox_hint.grid(row=5, column=1, sticky="w", padx=(10, 0))

        ttk.Label(form, text="提示詞", style="TLabel").grid(row=6, column=0, sticky="nw", pady=4)
        prompt_wrap = tk.Frame(form, bg=COLORS["border"])
        prompt_wrap.grid(row=6, column=1, sticky="ew", padx=(10, 0))
        self.prompt_text = tk.Text(
            prompt_wrap, height=5, wrap="word", bd=0, padx=8, pady=6,
            bg=COLORS["panel"], fg=COLORS["text"], font=(app.ui_font, 9), undo=True,
        )
        self.prompt_text.pack(fill="both", expand=True, padx=1, pady=1)
        self.prompt_hint = ttk.Label(form, text="", style="Muted.TLabel",
                                     wraplength=620, justify="left")
        self.prompt_hint.grid(row=7, column=1, sticky="w", padx=(10, 0), pady=(2, 0))

        actions = ttk.Frame(self, style="TFrame")
        actions.grid(row=3, column=0, sticky="w", pady=(18, 0))
        ttk.Button(actions, text="儲存設定", command=self.save).pack(side="left")
        ttk.Button(actions, text="新增對話", style="Toolbar.TButton",
                   command=self.app.new_conversation).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="新增子專案", style="Toolbar.TButton",
                   command=lambda: self.app.new_project(top_level=False)).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="開啟工作目錄", style="Toolbar.TButton",
                   command=self.open_cwd).pack(side="left", padx=(8, 0))

        ttk.Label(
            self,
            text="留空的欄位會沿用上層專案的設定；子專案與對話都會繼承這裡的設定。\n"
                 "「danger-full-access」與「no-sandbox」會讓 Codex 以你的身分執行指令、"
                 "讀寫不限於工作目錄 — 網路磁碟上的專案只能用這兩種。",
            style="Muted.TLabel",
            wraplength=620,
            justify="left",
        ).grid(row=4, column=0, sticky="w", pady=(16, 0))

        self.warn_label = ttk.Label(self, text="", style="Warn.TLabel", anchor="w", wraplength=620)
        self.warn_label.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.warn_label.grid_remove()

    def show(self, project: dict[str, Any]) -> None:
        self.project_id = project["id"]
        ws = self.app.ws
        self.title_label.configure(text=ws.path_of(project["id"]))
        subprojects = sum(1 for c in project["children"] if c["kind"] == store.PROJECT)
        conversations = sum(1 for c in project["children"] if c["kind"] == store.CONVERSATION)
        summary = f"{subprojects} 個子專案 · {conversations} 個對話"
        usage = ws.usage_of(project["id"])
        if usage.get("turns"):
            summary += "  ·  整棵子樹用量: {} 回 · in {:,} · out {:,}".format(
                usage.get("turns", 0), usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            )
        self.summary_label.configure(text=summary)

        self.cwd_var.set(project.get("cwd") or "")
        self.model_var.set(project.get("model") or "")
        self.sandbox_var.set(
            codex_runner.sandbox_label(project["sandbox"]) if project.get("sandbox") else INHERIT
        )
        self.cwd_hint.configure(text=f"繼承值：{ws.inherited(project['id'], 'cwd')}")
        self.model_hint.configure(
            text=f"繼承值：{ws.inherited(project['id'], 'model') or '(config 預設)'}"
        )
        self.sandbox_hint.configure(text=f"繼承值：{ws.inherited(project['id'], 'sandbox')}")

        self.prompt_text.delete("1.0", "end")
        if project.get("prompt"):
            self.prompt_text.insert("1.0", project["prompt"])
        inherited_prompt = ws.instructions_for(project["id"], include_self=False)
        if inherited_prompt:
            preview = inherited_prompt.replace("\n", " ")
            if len(preview) > 150:
                preview = preview[:150] + "…"
            self.prompt_hint.configure(
                text=f"會接在上層專案的提示詞之後（上層共 {len(inherited_prompt)} 字）：{preview}"
            )
        else:
            self.prompt_hint.configure(
                text="送出新對話的第一則訊息時，會把這段提示詞一起交給 Codex（之後由 thread 自己記著）。"
            )

        effective = ws.resolve(project["id"])
        warning = codex_runner.sandbox_warning(effective["cwd"], effective.get("sandbox"))
        if warning:
            self.warn_label.configure(text="⚠  " + warning)
            self.warn_label.grid()
        else:
            self.warn_label.grid_remove()

    def browse_cwd(self) -> None:
        initial = self.cwd_var.get() or (
            self.app.ws.inherited(self.project_id, "cwd") if self.project_id else ""
        )
        chosen = filedialog.askdirectory(initialdir=initial or None, parent=self)
        if chosen:
            self.cwd_var.set(os.path.normpath(chosen))

    def open_cwd(self) -> None:
        if self.project_id:
            _open_in_explorer(self.app.ws.resolve(self.project_id)["cwd"])

    def save(self) -> None:
        if not self.project_id:
            return
        cwd = self.cwd_var.get().strip()
        if cwd and not os.path.isdir(cwd):
            messagebox.showwarning(APP_NAME, f"目錄不存在：\n{cwd}", parent=self)
            return
        sandbox = self.sandbox_var.get()
        ws = self.app.ws
        ws.set_option(self.project_id, "cwd", cwd)
        ws.set_option(self.project_id, "model", self.model_var.get().strip())
        ws.set_option(self.project_id, "prompt", self.prompt_text.get("1.0", "end").strip())
        ws.set_option(
            self.project_id,
            "sandbox",
            "" if sandbox == INHERIT else codex_runner.sandbox_from_label(sandbox),
        )
        self.show(ws.find(self.project_id))
        self.app.set_status("已儲存專案設定")


class AgentsDialog(tk.Toplevel):
    """Workspace-wide executable configuration for the built-in runners."""

    _ROWS = (
        (store.CODEX_AGENT, "Codex CLI", codex_runner.find_codex, codex_runner.codex_version),
        (store.CLAUDE_AGENT, "Claude Code", codex_runner.find_claude, codex_runner.claude_version),
    )

    def __init__(self, master: tk.Misc, app: TreeAgentApp) -> None:
        super().__init__(master)
        self.app = app
        self.title("Agent 設定")
        self.transient(master)
        self.resizable(False, False)
        self.configure(bg=COLORS["bg"])
        self.vars: dict[str, tk.StringVar] = {}
        self.statuses: dict[str, ttk.Label] = {}
        frame = ttk.Frame(self, style="TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="內建 Agent", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            frame, text="留空時自動從系統 PATH 偵測。每個對話可在標題列選擇 Agent。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))
        for row, (agent_id, label, _find, _version) in enumerate(self._ROWS, start=2):
            ttk.Label(frame, text=label, style="TLabel").grid(row=row, column=0, sticky="w", pady=4)
            var = self.vars[agent_id] = tk.StringVar(value=app.ws.agent_path(agent_id) or "")
            ttk.Entry(frame, textvariable=var, width=52).grid(row=row, column=1, sticky="ew", padx=(10, 0))
            ttk.Button(frame, text="瀏覽…", style="Toolbar.TButton",
                       command=lambda aid=agent_id: self._browse(aid)).grid(row=row, column=2, padx=(6, 0))
            status = self.statuses[agent_id] = ttk.Label(frame, text="", style="Muted.TLabel")
            status.grid(row=row + 1, column=1, columnspan=2, sticky="w", padx=(10, 0))
        buttons = ttk.Frame(frame, style="TFrame")
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="重新偵測", command=self._refresh).pack(side="left", padx=(0, 12))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="儲存", command=self._save).pack(side="right")
        self._refresh()
        self.grab_set()

    def _browse(self, agent_id: str) -> None:
        path = filedialog.askopenfilename(parent=self, title=f"選擇 {AGENT_LABELS[agent_id]} 執行檔")
        if path:
            self.vars[agent_id].set(path)
            self._refresh()

    def _refresh(self) -> None:
        details = {item[0]: item for item in self._ROWS}
        for agent_id, (unused, label, finder, version) in details.items():
            path = self.vars[agent_id].get().strip() or None
            try:
                resolved = finder(path)
                detected = version(path) or "已偵測到"
                self.statuses[agent_id].configure(text=f"✓ {detected}  ·  {resolved}")
            except (codex_runner.CodexNotFound, codex_runner.ClaudeNotFound):
                self.statuses[agent_id].configure(text=f"未偵測到 {label}")

    def _save(self) -> None:
        for agent_id, _label, _finder, _version in self._ROWS:
            path = self.vars[agent_id].get().strip()
            if path and not os.path.isfile(path):
                messagebox.showwarning(APP_NAME, f"執行檔不存在：\n{path}", parent=self)
                return
        for agent_id, _label, _finder, _version in self._ROWS:
            self.app.ws.set_agent_path(agent_id, self.vars[agent_id].get())
        self.app.set_status("已儲存 Agent 設定")
        self.destroy()


class DefaultsDialog(tk.Toplevel):
    """Workspace-wide fallbacks, used when no project overrides them."""

    def __init__(self, master: tk.Misc, app: TreeAgentApp) -> None:
        super().__init__(master)
        self.app = app
        self.title("預設值設定")
        self.transient(master)
        self.resizable(False, False)
        self.configure(bg=COLORS["bg"])

        defaults = app.ws.defaults
        frame = ttk.Frame(self, style="TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="預設工作目錄", style="TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.cwd_var = tk.StringVar(value=defaults.get("cwd") or "")
        entry = ttk.Entry(frame, textvariable=self.cwd_var, width=52)
        entry.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        ttk.Button(frame, text="瀏覽…", style="Toolbar.TButton", command=self._browse).grid(
            row=0, column=2, padx=(6, 0)
        )

        ttk.Label(frame, text="預設模型", style="TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar(value=defaults.get("model") or "")
        ttk.Entry(frame, textvariable=self.model_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        ttk.Label(frame, text="預設沙箱模式", style="TLabel").grid(row=2, column=0, sticky="w", pady=4)
        self.sandbox_var = tk.StringVar(
            value=codex_runner.sandbox_label(
                defaults.get("sandbox") or store.DEFAULT_SANDBOX
            )
        )
        ttk.Combobox(
            frame,
            textvariable=self.sandbox_var,
            values=tuple(codex_runner.sandbox_label(m) for m in codex_runner.SANDBOX_MODES),
            state="readonly",
            width=46,
        ).grid(row=2, column=1, columnspan=2, sticky="w", padx=(10, 0))

        buttons = ttk.Frame(frame, style="TFrame")
        buttons.grid(row=3, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="儲存", command=self._save).pack(side="right")

        entry.focus_set()
        self.grab_set()

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.cwd_var.get() or None, parent=self)
        if chosen:
            self.cwd_var.set(os.path.normpath(chosen))

    def _save(self) -> None:
        cwd = self.cwd_var.get().strip() or store.default_cwd()
        if not os.path.isdir(cwd):
            messagebox.showwarning(APP_NAME, f"目錄不存在：\n{cwd}", parent=self)
            return
        self.app.ws.defaults.update(
            {
                "cwd": cwd,
                "model": self.model_var.get().strip() or None,
                "sandbox": codex_runner.sandbox_from_label(self.sandbox_var.get()),
            }
        )
        self.app.ws.save()
        self.app.on_tree_select()
        self.app.set_status("已儲存預設值")
        self.destroy()


class _Tooltip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip, text=self.text, bg=COLORS["tooltip"], fg="white", padx=8, pady=3,
            font=("Segoe UI", 8),
        ).pack()

    def _hide(self, _event=None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


def _safe_filename(name: str) -> str:
    """A node name reduced to something a filesystem will accept."""
    cleaned = "".join("_" if ch in r'<>:"/\|?*' else ch for ch in name).strip(" .")
    return (cleaned or "export")[:80]


def _iter_subtree(node: dict[str, Any]):
    yield node
    if node["kind"] == store.PROJECT:
        for child in node["children"]:
            yield from _iter_subtree(child)


def _open_in_explorer(path: str) -> None:
    if not path or not os.path.isdir(path):
        messagebox.showwarning(APP_NAME, f"目錄不存在：\n{path}")
        return
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 - user-initiated
    else:
        subprocess.Popen(["xdg-open", path])


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="tree_agent", description=f"{APP_NAME} — Codex CLI GUI")
    parser.add_argument(
        "--home",
        default=store.DEFAULT_HOME,
        help="工作區資料夾（預設 ~/.tree_agent），可用來分開多組工作區",
    )
    args = parser.parse_args(argv)

    _enable_dpi_awareness()
    root = tk.Tk()
    TreeAgentApp(root, home=args.home)
    root.mainloop()


if __name__ == "__main__":
    main()
