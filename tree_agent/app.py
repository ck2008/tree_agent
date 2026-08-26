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
import hashlib
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any
from urllib.parse import quote, unquote, urlparse

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Keep the main UI usable if optional drag support is absent.
    DND_FILES = None
    TkinterDnD = None

if __package__ in (None, ""):  # allow `python app.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tree_agent import clipboard_image, codex_runner, pdf_support, richtext, store, transfer
else:
    from . import clipboard_image, codex_runner, pdf_support, richtext, store, transfer

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
    "activity": "#eef0f3",
    "sidebar": "#ffffff",
    "editor": "#ffffff",
    "toolbar": "#f6f7f9",
    "input": "#ffffff",
    "hover": "#e8f0fe",
    "button": "#ffffff",
    "button_hover": "#e8f0fe",
    "primary": "#1a56db",
    "primary_hover": "#1649ba",
    "focus": "#1a56db",
}

DARK_COLORS = {
    # Warm neutral surfaces with an off-white body text, rather than the cool
    # near-black and dim grey of Dark+.  Pure-grey darks read as a switched-off
    # screen next to the warm light theme, and #d4d4d4 body text on #1e1e1e is
    # dimmer than it needs to be for long transcripts.  Only the neutrals moved;
    # the semantic accents (你 / 錯誤 / 提示 / 選取) are unchanged.
    #
    # The chrome is deliberately *flat*: the rail, the menu bar, the explorer
    # and the transcript are all one colour, and only a raised card (`tool_bg`,
    # `user_bg`) lifts off it.  Dark+ layers them the other way round -- an
    # explorer lighter than the editor -- which reads as a stack of panels
    # rather than one surface.
    #
    # A consequence worth knowing: `_replace_widget_colors` repaints live
    # widgets by mapping old-palette-value -> new-palette-value, so the roles
    # sharing one value here cannot be told apart on a theme switch (for a
    # duplicate, the last key wins).  Every surface that needs a role the
    # mapping cannot infer is repainted by name at the end of `apply_theme`.
    "bg": "#262624",
    "panel": "#262624",
    "border": "#3b3a36",
    "text": "#e8e6e1",
    "muted": "#a3a09a",
    "user": "#4daafc",
    "agent": "#e8e6e1",
    "reasoning": "#a3a09a",
    "tool": "#d5d2cc",
    "tool_bg": "#2f2e2c",
    "error": "#f14c4c",
    "notice": "#cca700",
    "accent": "#3794ff",
    "user_bg": "#363431",
    "warn_bg": "#383018",
    "select": "#094771",
    "select_idle": "#3f3d39",
    "tree_conversation": "#d5d2cc",
    "drop_target": "#007fd4",
    "log": "#a3a09a",
    "tooltip": "#141312",
    "activity": "#262624",
    "sidebar": "#262624",
    "editor": "#262624",
    "toolbar": "#262624",
    "input": "#3b3a36",
    "hover": "#363431",
    "button": "#3b3a36",
    "button_hover": "#4a4843",
    "primary": "#0e639c",
    "primary_hover": "#1177bb",
    "focus": "#007fd4",
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
PDF_ONLY_PROMPT = "請分析附加的 PDF。"
PDF_PAGE_LIMIT = 20
PDF_TEXT_LIMIT = 60_000
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
_TERMINAL_LOG_LINE = re.compile(
    r"^\s*(?:at\s+|Error\b|Traceback\b|Caused by:|npm WARN\b|file:|"
    r"[A-Za-z_][\w.]*?(?:Error|Exception):)|\b(?:EACCES|EBADENGINE|"
    r"node:internal/)\b",
    re.MULTILINE,
)
_FENCED_CODE_BLOCK = re.compile(
    r"(?ms)^[ \t]*(```|~~~)[^\r\n]*\r?\n(.*?)^[ \t]*\1[ \t]*$"
)

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
MIN_PROJECT_PANE_PX = 220
BUBBLE_MAX_RATIO = 0.72       # your own message never spans the whole measure
BLOCK_PADX_PX = 12            # window_create padding either side of a card
BLOCK_RMARGIN_PX = 14         # matches the `user` tag, so both edges line up
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
_FILE_CITATION = re.compile(r':codex-file-citation\{path="([^"]+)"[^}]*\}')


def _file_citations_to_markdown(text: str) -> str:
    """Turn an agent file citation into a clickable local Markdown link."""
    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        label = os.path.basename(path.replace("\\", "/")) or path
        url = "file:///" + quote(path.replace("\\", "/"), safe="/:")
        return f"[{label}]({url})"
    return _FILE_CITATION.sub(replace, text)


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
        # Tool calls are audit data, not part of the answer.  Keep them in
        # the information rail and never render them in the central transcript.
        self.tool_display = TOOL_HIDDEN
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
        style.configure("Sidebar.TFrame", background=COLORS["sidebar"])
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
        style.configure("Treeview", rowheight=24 if self.theme == THEME_DARK else 26, font=(self.ui_font, 10), background=COLORS["sidebar"],
                        fieldbackground=COLORS["sidebar"], foreground=COLORS["text"], bordercolor=COLORS["border"],
                        lightcolor=COLORS["border"], darkcolor=COLORS["border"])
        style.map("Treeview", background=[("selected", COLORS["select"]), ("active", COLORS["hover"])], foreground=[("selected", "#ffffff")])
        style.configure("TButton", background=COLORS["button"], foreground=COLORS["text"], bordercolor=COLORS["border"], relief="flat")
        style.map("TButton", background=[("active", COLORS["button_hover"])], foreground=[("disabled", COLORS["muted"])])
        # Vista ignores a custom ttk button background in light mode.  Its
        # default face stays white, so a white primary label disappears.  Use
        # a dark label there; the colourable Dark+ (clam) button keeps white.
        primary_foreground = "#ffffff" if self.theme == THEME_DARK else COLORS["text"]
        style.configure(
            "Primary.TButton", background=COLORS["primary"], foreground=primary_foreground,
            bordercolor=COLORS["primary"], padding=(8, 4),
        )
        # The Windows Vista renderer can make a disabled primary button's
        # label white on a white surface.  Use an explicit neutral fill and
        # readable foreground so an empty composer still shows 「送出 Enter」.
        style.map(
            "Primary.TButton",
            background=[("disabled", COLORS["tool_bg"]), ("active", COLORS["primary_hover"])],
            foreground=[("disabled", COLORS["text"])],
            bordercolor=[("disabled", COLORS["border"])],
        )
        style.configure("TEntry", fieldbackground=COLORS["input"], foreground=COLORS["text"], bordercolor=COLORS["border"])
        style.configure("TCombobox", fieldbackground=COLORS["input"], foreground=COLORS["text"], background=COLORS["input"], bordercolor=COLORS["border"])
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["input"])], foreground=[("readonly", COLORS["text"])])
        style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TRadiobutton", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TPanedwindow", background=COLORS["border"])
        # Keep every vertical scrollbar on the same palette.  Giving this a
        # named style avoids the Explorer tree falling back to Windows' light
        # scrollbar while the transcript is rendered with the clam theme.
        style.configure(
            "VS.Vertical.TScrollbar", background=COLORS["button"],
            troughcolor=COLORS["editor"], bordercolor=COLORS["editor"],
            arrowcolor=COLORS["muted"], width=11,
        )
        style.map("VS.Vertical.TScrollbar", background=[("active", COLORS["button_hover"])])
        style.configure("Toolbar.TButton", padding=(8, 3))
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

        # Menus belong to the root so they can be used both by the native
        # menu bar (light mode) and as independent popups (dark mode).
        file_menu = tk.Menu(self.root, tearoff=0)
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

        edit_menu = tk.Menu(self.root, tearoff=0)
        self._menus.append(edit_menu)
        edit_menu.add_command(label="重新命名  (F2)", command=self.rename_selected)
        edit_menu.add_command(label="刪除  (Del)", command=self.delete_selected)
        edit_menu.add_separator()
        edit_menu.add_command(label="複製對話內容", command=self.copy_transcript)
        edit_menu.add_command(label="從這裡分岔出新對話", command=self.fork_conversation)
        edit_menu.add_command(label="審查未提交的變更", command=self.review_changes)
        edit_menu.add_command(label="重設對話（清空並開新 thread）", command=self.reset_conversation)
        menubar.add_cascade(label="編輯", menu=edit_menu)

        view_menu = tk.Menu(self.root, tearoff=0)
        self._menus.append(view_menu)
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

        help_menu = tk.Menu(self.root, tearoff=0)
        self._menus.append(help_menu)
        help_menu.add_command(label="關於", command=self.show_about)
        menubar.add_cascade(label="說明", menu=help_menu)

        self.menubar = menubar
        self.custom_menu_bar = tk.Frame(self.root, bg=COLORS["toolbar"], height=28)
        self.custom_menu_bar.pack_propagate(False)
        self.custom_menu_buttons: list[tk.Button] = []
        for label, menu in (("檔案", file_menu), ("編輯", edit_menu), ("檢視", view_menu), ("說明", help_menu)):
            # Menubutton's native menu binding does not open reliably when a
            # Menu was also attached to the hidden Windows menu bar.  Posting
            # the very same menu explicitly makes the dark title-bar controls
            # behave like ordinary menu buttons.
            button = tk.Button(
                self.custom_menu_bar, text=label, bd=0, relief="flat",
                bg=COLORS["toolbar"], fg=COLORS["text"], activebackground=COLORS["hover"],
                activeforeground=COLORS["text"], font=(self.ui_font, 9), padx=9,
            )
            button.configure(command=lambda m=menu, b=button: self._post_menu(m, b))
            button.pack(side="left", fill="y")
            self.custom_menu_buttons.append(button)
        self._configure_menus()
        self._apply_menu_mode()
        self.root.bind_all("<Control-f>", lambda e: self.focus_search())
        self.root.bind_all("<Control-F>", lambda e: self.focus_search())

    @staticmethod
    def _post_menu(menu: tk.Menu, button: tk.Widget) -> None:
        """Open an in-window menu beneath its dark title-bar button."""
        # ``post`` leaves normal mouse handling to Tk and returns straight
        # away.  ``tk_popup`` can block a Button command on Windows when the
        # original native menu bar has been detached for dark mode.
        menu.post(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def _apply_menu_mode(self) -> None:
        """Use an in-window dark menu bar: Windows native menus ignore colours."""
        if self.theme == THEME_DARK:
            self.root.config(menu="")
            if not self.custom_menu_bar.winfo_ismapped():
                options: dict[str, Any] = {"fill": "x", "side": "top"}
                if hasattr(self, "outer"):
                    options["before"] = self.outer
                self.custom_menu_bar.pack(**options)
        else:
            self.custom_menu_bar.pack_forget()
            self.root.config(menu=self.menubar)

    # ------------------------------------------------------------- layout

    def _build_layout(self) -> None:
        self.outer = outer = ttk.Frame(self.root, style="TFrame")
        outer.pack(fill="both", expand=True)

        # A single functional VS Code-style activity rail.  It focuses the
        # workspace explorer rather than pretending this app has extra modes.
        self.activity_rail = tk.Frame(outer, bg=COLORS["activity"], width=44)
        self.activity_rail.pack_propagate(False)
        self.activity_button = tk.Button(
            self.activity_rail, text="▦", bd=0, relief="flat", padx=0, pady=8,
            bg=COLORS["activity"], fg=COLORS["accent"], activebackground=COLORS["hover"],
            activeforeground=COLORS["accent"], font=(self.ui_font, 16),
            command=lambda: self.tree.focus_set(),
        )
        self.activity_button.pack(fill="x", pady=(4, 0))

        self.paned = ttk.PanedWindow(outer, orient="horizontal")

        self._build_tree_pane()
        self._build_detail_pane()

        ui = self.ws.data.get("ui") or {}
        # Restore only after the pane has a real width.  A fixed timer races
        # with Windows' first layout pass and can leave the project explorer
        # collapsed at x=0 on startup.
        self._startup_sash = int(ui.get("sash", 300))
        self._startup_sash_restored = False
        self.paned.bind("<Configure>", self._restore_startup_sash, add="+")
        self._sash_job = self.root.after_idle(self._restore_startup_sash)

        self.status = ttk.Label(outer, text="", style="Muted.TLabel", anchor="w")
        # The main paned window was packed to the left while this label used
        # Tk's default top side.  Pack then allocated a right-hand strip to
        # the status label, leaving a permanent empty area beside the info
        # rail.  Reserving the status row at the bottom lets the workspace
        # (and therefore the info panel) use the full remaining width.
        self.status.pack(side="bottom", fill="x", padx=8, pady=(3, 4))
        self.paned.pack(side="left", fill="both", expand=True)
        version = codex_runner.codex_version()
        self.set_status(f"就緒 · {version}" if version else "找不到 codex CLI，請確認已安裝並在 PATH 中")
        self._apply_theme_layout()

    def _apply_theme_layout(self) -> None:
        if self.theme == THEME_DARK:
            if not self.activity_rail.winfo_ismapped():
                self.activity_rail.pack(side="left", fill="y", before=self.paned)
            self.paned.pack_configure(padx=0, pady=0)
        else:
            self.activity_rail.pack_forget()
            self.paned.pack_configure(padx=8, pady=(8, 4))

    def _set_sash(self, position: int) -> None:
        try:
            width = self.paned.winfo_width()
            if width < 1:
                return
            # Never restore a collapsed project pane.  Older versions could
            # persist a zero sash while the window was closing, making the
            # explorer appear to vanish on the next launch.
            maximum = max(0, width - MIN_CONTENT_PX)
            minimum = min(MIN_PROJECT_PANE_PX, maximum)
            self.paned.sashpos(0, max(minimum, min(int(position), maximum)))
        except tk.TclError:
            pass

    def _restore_startup_sash(self, _event=None) -> None:
        """Apply the saved main-sash position once the paned window is ready."""
        if self._startup_sash_restored:
            return
        if self.paned.winfo_width() < MIN_PROJECT_PANE_PX + MIN_CONTENT_PX:
            return
        self._set_sash(self._startup_sash)
        self._startup_sash_restored = True

    def _build_tree_pane(self) -> None:
        left = ttk.Frame(self.paned, style="Sidebar.TFrame")
        self.paned.add(left, weight=0)

        bar = ttk.Frame(left, style="Sidebar.TFrame")
        bar.pack(fill="x", pady=(4, 4))
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
        search_row.pack(fill="x", pady=(0, 4))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            search_row, textvariable=self.search_var, bd=0, bg=COLORS["input"],
            fg=COLORS["text"], font=(self.ui_font, 9), insertbackground=COLORS["text"],
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=3)
        self.search_clear = tk.Button(
            search_row, text="✕", bd=0, bg=COLORS["input"], fg=COLORS["muted"],
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

        holder = ttk.Frame(left, style="Sidebar.TFrame")
        holder.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(holder, show="tree", selectmode="browse")
        vsb = ttk.Scrollbar(
            holder, orient="vertical", command=self.tree.yview,
            style="VS.Vertical.TScrollbar",
        )
        self.tree_scrollbar = vsb
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
        self._apply_menu_mode()
        self._replace_widget_colors(self.root, old)
        self._apply_theme_layout()
        # Several light surfaces intentionally share white; restore the
        # distinct Dark+ layers that a one-to-one colour replacement cannot
        # infer from those duplicate source values.
        self.search_entry.configure(bg=COLORS["input"])
        self.search_clear.configure(bg=COLORS["input"])
        self.conv_view.text.configure(bg=COLORS["editor"])
        self.conv_view.input.configure(bg=COLORS["input"])
        self.conv_view.outline.configure(bg=COLORS["sidebar"])
        self.conv_view.outline_body.configure(bg=COLORS["sidebar"])
        self.conv_view.info.configure(bg=COLORS["sidebar"])
        self.conv_view.info_body.configure(bg=COLORS["sidebar"])
        self.tree.tag_configure("project", foreground=COLORS["text"])
        self.tree.tag_configure("conversation", foreground=COLORS["tree_conversation"])
        self.tree.tag_configure("running", foreground=COLORS["accent"])
        self.tree.tag_configure("droptarget", background=COLORS["drop_target"])
        self.conv_view._configure_tags()
        self.conv_view.recolor_bubbles()
        # The flat dark chrome gives the rail, the menu bar, the explorer and
        # the transcript one shared value, so the value-for-value mapping cannot
        # tell which role each of them meant. Name them.
        self.custom_menu_bar.configure(bg=COLORS["toolbar"])
        for button in self.custom_menu_buttons:
            button.configure(bg=COLORS["toolbar"], fg=COLORS["text"],
                             activebackground=COLORS["hover"], activeforeground=COLORS["text"])
        self.activity_rail.configure(bg=COLORS["activity"])
        self.activity_button.configure(bg=COLORS["activity"], fg=COLORS["accent"],
                                       activebackground=COLORS["hover"],
                                       activeforeground=COLORS["accent"])
        # Both rails are built from labels whose colours are picked at build
        # time; rebuilding them is cheaper to keep right than mapping each one.
        # `None` until the first layout pass decides which rails fit.
        outline, info = self.conv_view._panels_shown or (False, False)
        if outline:
            self.conv_view.refresh_outline()
        if info:
            self.conv_view.refresh_info()
        self.ws.data.setdefault("ui", {})["theme"] = self.theme
        self.ws.touch()
        self.set_status("已切換為" + ("深色模式" if self.theme == THEME_DARK else "淺色模式"))

    def apply_tool_display(self) -> None:
        """Compatibility hook: central transcripts always hide tool calls."""
        self.tool_display = TOOL_HIDDEN
        self.ws.data.setdefault("ui", {})["tool_display"] = self.tool_display
        self.ws.touch()
        conv = self.ws.find(self.current_id)
        if conv is not None and conv["kind"] == store.CONVERSATION:
            offset = self.conv_view.text.yview()[0]
            self.conv_view.show(conv)
            self.conv_view.text.yview_moveto(offset)
        self.set_status("工具紀錄會顯示在右側資訊面板")

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
        page_limit = PDF_PAGE_LIMIT
        if any(path.lower().endswith(".pdf") for path in images):
            selected = simpledialog.askinteger(
                APP_NAME, "PDF 最多處理幾頁？", initialvalue=PDF_PAGE_LIMIT,
                minvalue=1, parent=self.root,
            )
            if selected is None:
                return
            page_limit = selected
        prepared = self._prepare_attachments(images, page_limit=page_limit)
        if prepared["errors"]:
            messagebox.showwarning(APP_NAME, "\n".join(prepared["errors"]), parent=self.root)
            return

        # Project instructions ride along with the first message of a new
        # thread; from then on the thread itself carries them, so resending
        # would just burn tokens. A fork already inherits its source's context.
        instructions = ""
        has_context = conv.get("claude_session_id") if agent_id == store.CLAUDE_AGENT else (conv.get("thread_id") or conv.get("fork_of"))
        if not has_context:
            instructions = self.ws.instructions_for(conv_id)
        prompt_with_attachments = prompt + prepared["context"]
        outgoing = (f"{instructions}\n\n---\n\n{prompt_with_attachments}"
                    if instructions else prompt_with_attachments)
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
                permission_mode=settings.get("claude_permission"),
                add_dirs=prepared["claude_dirs"],
            )
        else:
            turn = codex_runner.Turn(
                prompt=outgoing, cwd=settings["cwd"], emit=emit,
                thread_id=conv.get("thread_id"), model=settings.get("model"),
                sandbox=settings.get("sandbox"), fork_from=conv.get("fork_of"),
                images=prepared["codex_images"], review=review,
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

    def _prepare_attachments(self, paths: list[str], *, page_limit: int = PDF_PAGE_LIMIT) -> dict[str, Any]:
        """Turn PDF attachments into text plus page images for both CLIs."""
        codex_images: list[str] = []
        claude_dirs: list[str] = []
        context: list[str] = []
        errors: list[str] = []
        for path in paths:
            if not os.path.isfile(path):
                errors.append(f"附件已不存在：{path}")
                continue
            suffix = os.path.splitext(path)[1].lower()
            folder = os.path.dirname(os.path.abspath(path))
            if folder not in claude_dirs:
                claude_dirs.append(folder)
            if suffix in _IMAGE_SUFFIXES:
                codex_images.append(path)
                context.append(f"\n\n附件圖片：{path}\n")
                continue
            if suffix != ".pdf":
                errors.append(f"不支援的附件格式：{os.path.basename(path)}")
                continue

            # Keep rendered images in the workspace, rather than beside the
            # user's source PDF, so they survive long enough for the CLI turn.
            key = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
            output_dir = os.path.join(self.ws.home, "pdf-pages", key)
            inspected = pdf_support.inspect_pdf(path, output_dir, page_limit=page_limit)
            if not inspected.text and not inspected.rendered_images:
                detail = "；".join(inspected.errors) or "無法讀取 PDF"
                errors.append(f"{os.path.basename(path)}：{detail}")
                continue
            codex_images.extend(inspected.rendered_images)
            if output_dir not in claude_dirs:
                claude_dirs.append(output_dir)
            page_note = (
                f"\n\nPDF 附件：{path}\n"
                f"共 {inspected.page_count} 頁；已處理前 {min(inspected.page_count, page_limit)} 頁。\n"
            )
            if inspected.text:
                extracted = inspected.text[:PDF_TEXT_LIMIT]
                if len(inspected.text) > PDF_TEXT_LIMIT:
                    extracted += "\n[PDF 文字過長，已截斷]"
                page_note += "擷取文字：\n" + extracted + "\n"
            if inspected.rendered_images:
                page_note += "頁面圖片：\n" + "\n".join(inspected.rendered_images) + "\n"
            # Claude Code receives the text above and can use Read on these
            # paths because their directories are supplied with --add-dir.
            context.append(page_note)
        return {
            "codex_images": codex_images,
            "claude_dirs": claude_dirs,
            "context": "".join(context),
            "errors": errors,
        }

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
        if prompt in (IMAGE_ONLY_PROMPT, PDF_ONLY_PROMPT) and images:
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
                if role == "tool":
                    self.conv_view.refresh_info()
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
            ui["sash"] = max(MIN_PROJECT_PANE_PX, self.paned.sashpos(0))
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
        self._shown_agent_labels: set[str] = set()

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
            bg=COLORS["editor"],
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
        vsb = ttk.Scrollbar(
            body, orient="vertical", command=self.text.yview,
            style="VS.Vertical.TScrollbar",
        )
        self._vsb = vsb
        self.text.configure(yscrollcommand=self._on_scroll)
        self.text.bind("<Configure>", self._resize_inline_blocks, add="+")
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
            bg=COLORS["input"],
            fg=COLORS["text"],
            font=(app.ui_font, 10),
            undo=True,
        )
        self.input.pack(fill="both", expand=True, padx=1, pady=1)
        self.input.bind("<Return>", self._on_send_key)
        self.input.bind("<Shift-Return>", lambda e: None)  # let Text insert a newline
        self.input.bind("<KeyRelease>", lambda e: self._refresh_send_state())
        self._register_file_drop()

        side = ttk.Frame(composer, style="TFrame")
        side.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.send_button = ttk.Button(side, text="送出\nEnter", style="Primary.TButton", command=self.on_send, width=12)
        self.send_button.pack(fill="x")
        self.stop_button = ttk.Button(side, text="停止", command=self.on_stop, width=12, state="disabled")
        self.stop_button.pack(fill="x", pady=(6, 0))
        self.attach_button = ttk.Button(side, text="附加檔案", style="Toolbar.TButton",
                                        command=self.attach_files, width=12)
        self.attach_button.pack(fill="x", pady=(6, 0))
        _Tooltip(self.attach_button, "附加圖片或 PDF（也可以直接在輸入框按 Ctrl+V 貼上截圖）")

        self._bind_transcript()  # needs self.input, so it runs last
        self._refresh_send_state()

    # ------------------------------------------------------- side panels

    def _build_outline(self) -> None:
        """The navigation rail: your questions and Codex's headings."""
        self.outline = tk.Frame(self.splitter, bg=COLORS["sidebar"],
                                width=self.app.outline_width)
        # The children are packed, so pack_propagate is what stops them forcing
        # the rail wider than the sash puts it.
        self.outline.pack_propagate(False)
        ttk.Label(self.outline, text="大綱", style="Sidebar.Section.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        self.outline_body = tk.Frame(self.outline, bg=COLORS["sidebar"])
        self.outline_body.pack(fill="both", expand=True)
        self._outline_targets: list[str] = []

    def _build_info(self) -> None:
        """The details panel: settings, usage, attachments, changed files."""
        self.info = tk.Frame(self.splitter, bg=COLORS["sidebar"],
                             width=self.app.info_width)
        self.info.pack_propagate(False)
        ttk.Label(self.info, text="資訊", style="Sidebar.Section.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        self.info_body = tk.Frame(self.info, bg=COLORS["sidebar"])
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

        # Your turns are embedded bubbles now, so their headings are recorded as
        # they are drawn rather than read back out of the widget.
        for index, label in getattr(self, "_user_entries", ()):
            found.append((key(index), label or "（訊息）", 0, index))

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
                bg=COLORS["sidebar"],
                fg=COLORS["user"] if depth == 0 else COLORS["muted"],
                font=(self.app.ui_font, 9, "bold" if depth == 0 else "normal"),
                anchor="w", justify="left", cursor="hand2", padx=2,
            )
            row.pack(fill="x")
            row.bind("<Button-1>", lambda e, i=index: self.jump_to(i))
            row.bind("<Enter>", lambda e, w=row: w.configure(bg=COLORS["user_bg"]))
            row.bind("<Leave>", lambda e, w=row: w.configure(bg=COLORS["sidebar"]))
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
            tk.Label(self.info_body, text=value, bg=COLORS["sidebar"], fg=COLORS["text"],
                     font=(self.app.mono_font, 8), anchor="w", justify="left",
                     wraplength=INFO_WIDTH - 16).pack(anchor="w", pady=(0, 6))

        images = [p for m in conv["messages"] for p in (m.get("images") or ())]
        if images:
            ttk.Label(self.info_body, text=f"附件（{len(images)}）",
                      style="Muted.TLabel").pack(anchor="w", pady=(4, 2))
            strip = tk.Frame(self.info_body, bg=COLORS["sidebar"])
            strip.pack(anchor="w")
            for path in images[:6]:
                thumb = self._thumbnail(path)
                if thumb is None:
                    continue
                self._info_thumbs.append(thumb)
                holder = tk.Label(strip, image=thumb, bg=COLORS["sidebar"], cursor="hand2")
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
                tk.Label(self.info_body, text=entry, bg=COLORS["sidebar"], fg=COLORS["tool"],
                         font=(self.app.mono_font, 8), anchor="w", justify="left",
                         wraplength=INFO_WIDTH - 16).pack(anchor="w")

        tool_events = [
            m["text"] for m in conv["messages"]
            if m["role"] in ("tool", "agent_tool")
        ]
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
            bar = ttk.Scrollbar(holder, orient="vertical", command=log.yview,
                                style="VS.Vertical.TScrollbar")
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
        self.text.tag_bind("md_link", "<Button-1>", self._markdown_link_menu)
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

    def _register_file_drop(self) -> None:
        """Accept PDF files dropped directly on the message input on Windows."""
        root = self.winfo_toplevel()
        if (DND_FILES is None or not hasattr(self.input, "drop_target_register")
                or not hasattr(root, "TkdndVersion")):
            return
        self.input.drop_target_register(DND_FILES)
        self.input.dnd_bind("<<Drop>>", self._on_file_drop)

    def _on_file_drop(self, event) -> str:
        """Attach dropped PDFs without inserting their paths into the message."""
        paths = [os.path.abspath(path) for path in self.tk.splitlist(event.data)]
        pdfs = [path for path in paths if path.lower().endswith(".pdf")]
        if pdfs:
            self.add_attachments(pdfs)
        skipped = len(paths) - len(pdfs)
        if skipped:
            self.app.set_status("拖曳到輸入框僅支援 PDF 檔案")
        return "break"

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
            title="附加檔案",
            filetypes=[
                ("圖片或 PDF", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.pdf"),
                ("PDF", "*.pdf"),
                ("圖片", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                ("所有檔案", "*.*"),
            ],
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
            self.app.set_status(f"已附加 {added} 個檔案，送出時一併交給 Agent")

    def remove_attachment(self, path: str) -> None:
        current = self.current_attachments()
        if path in current:
            current.remove(path)
            self.refresh_attachments()

    def _thumbnail(self, path: str) -> tk.PhotoImage | None:
        """A small preview for image and PDF attachments when available."""
        source = path
        if path.lower().endswith(".pdf"):
            key = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
            rendered = pdf_support.render_pdf_thumbnail(
                path, os.path.join(self.app.ws.home, "pdf-thumbnails", key)
            )
            if not rendered.rendered_images:
                return None
            source = rendered.rendered_images[0]
        try:
            image = tk.PhotoImage(master=self, file=source)
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
                tk.Label(inner, text="PDF" if path.lower().endswith(".pdf") else "🖼", bg=COLORS["panel"],
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
        menu.add_command(label="附加檔案…", command=self.attach_files)
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
            "role_user_log", foreground=COLORS["user"], font=(app.ui_font, 10, "bold"),
            spacing1=12, justify="left", lmargin1=12, lmargin2=12,
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
        # Your message is drawn as an embedded bubble, which holds no transcript
        # text of its own. The words are inserted here as well and elided, the
        # same trick collapsed tool output uses, so Ctrl+A, Ctrl+C and search
        # still reach them. Justification matches `user`: an elided run shares a
        # display line with the bubble that follows it.
        self.text.tag_configure(
            "user_hidden", elide=True, justify="right", lmargin1=90, lmargin2=90,
            rmargin=14,
        )
        self.text.tag_configure(
            "user_log", foreground=COLORS["tool"], font=(app.mono_font, 9),
            justify="left", lmargin1=12, lmargin2=12, rmargin=12,
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
        self._inline_log_widgets: list[tk.Frame] = []
        self._inline_code_widgets: list[tk.Frame] = []
        self._inline_code_labels: list[tk.Label] = []
        self._inline_bubbles: list[tk.Label] = []
        self._user_entries: list[tuple[str, str]] = []
        self._link_targets: dict[str, str] = {}
        self._tool_blocks: list[tuple[str, str, str]] = []
        self._shown_agent_labels = set()
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
        self._resize_inline_blocks()
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
        text = _file_citations_to_markdown(text)
        # Hidden tool calls never leave a visible marker in the transcript.
        if role in ("tool", "agent_tool"):
            return
        if images:
            # Older transcripts baked the file names into the message text;
            # they are drawn from `images` now, so drop the duplicated lines.
            text = _ATTACHMENT_LINE.sub("", text).rstrip()
        user_log = role == "user" and _is_terminal_log(text)
        role_tag = (
            "role_user_log" if user_log
            else "role_agent" if role == "agent" else "role_other"
        )
        body_tag = (
            "user_log" if user_log else role
            if role in ("user", "agent", "reasoning", "tool", "error", "notice", "meta")
            else "agent"
        )
        if user_log:
            self.text.insert("end", "你（貼上的日誌）\n", role_tag)
            self._write_user_log(text)
        elif role == "user":
            # No "你" label above your own messages: the right-aligned bubble
            # already says whose turn it is, so the label was only a line of
            # noise repeated down the whole transcript.
            if text:
                self._write_user_bubble(text.rstrip())
        elif role == "agent":
            label = AGENT_LABELS.get(agent_id or self.app.ws.conversation_agent(self.conv_id or ""))
            if label not in self._shown_agent_labels:
                self.text.insert("end", label + "\n", role_tag)
                self._shown_agent_labels.add(label)
            # Only the agent's prose is Markdown; command output and the user's
            # own text are shown exactly as written.
            self._write_agent_markdown(text.strip(), body_tag)
        else:
            if role != "meta":
                self.text.insert("end", ROLE_LABELS.get(role, role) + "\n", role_tag)
            if text:
                self.text.insert("end", text.rstrip() + "\n", body_tag)
        for path in images or ():
            self._write_attachment(path, body_tag)

    def _write_user_bubble(self, text: str) -> None:
        """Draw your own message as a right-aligned tinted bubble.

        A tag background is painted across the whole display line rather than
        behind the glyphs, so tinting the `user` tag gave a full-width band and
        the transcript settled for alignment alone.  An embedded widget is sized
        by its own content, which is what finally makes a bubble possible.  It
        sits on a line carrying the right-justifying `user` tag, so the bubble
        hugs the right edge while the gutter beside it stays ordinary transcript
        that still takes a click.
        """
        bubble = tk.Frame(self.text, bg=COLORS["user_bg"], bd=0, highlightthickness=0)
        body = tk.Label(
            bubble, text=text, bg=COLORS["user_bg"], fg=COLORS["text"],
            font=(self.app.ui_font, 10), justify="left", anchor="w",
            padx=12, pady=8, wraplength=self._bubble_wraplength(),
        )
        body.pack(fill="both", expand=True)
        # A Label is not selectable, so the message stays reachable by menu.
        for widget in (bubble, body):
            widget.bind("<Button-3>", lambda event, value=text: self._bubble_menu(event, value))
        self._bind_block_mousewheel(bubble)
        self._inline_bubbles.append(body)
        start = self.text.index("end-1c")
        # Elided first, then the bubble: the transcript must not *end* on elided
        # text, because `following_tail()` asks the widget whether the last
        # character is on screen and an elided one never is.
        self.text.insert("end", text + "\n", "user_hidden")
        window_at = self.text.index("end-1c")
        self.text.window_create("end", window=bubble, padx=0, pady=3, align="top")
        self.text.insert("end", "\n", "user")
        self.text.tag_add("user", window_at, f"{window_at}+1c")
        # The outline is read back off the widget and an embedded window has no
        # text to read, so remember what this bubble stands for.
        first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
        self._user_entries.append((start, first_line))

    def recolor_bubbles(self) -> None:
        """Re-tint the bubbles already on screen after a theme change.

        `user_bg` shares its light value with `hover` and `button_hover`, so the
        value-for-value palette swap cannot tell which role a bubble meant and
        picks the wrong Dark+ layer. Setting it from the palette is exact.
        """
        for label in getattr(self, "_inline_bubbles", ()):
            if label.winfo_exists():
                label.configure(bg=COLORS["user_bg"], fg=COLORS["text"])
                parent = label.master
                if parent.winfo_exists():
                    parent.configure(bg=COLORS["user_bg"])

    def _content_px(self) -> int:
        """The readable measure: what is left between the transcript's insets.

        `winfo_width()` is the whole widget; the cap is applied as `padx`, so the
        inset has to come off both sides. Every embedded block is measured
        against this, otherwise they disagree about where the right edge is —
        a card sized to the *pane* overshoots the measure on a wide window and
        is clipped, while the bubbles stop at the measure and look misplaced.
        """
        width = self.text.winfo_width() - 2 * int(self.text.cget("padx"))
        return width if width > 240 else MAX_CONTENT_PX

    def _bubble_wraplength(self) -> int:
        """Pixels a bubble may fill before wrapping — never the whole measure."""
        return max(200, int(self._content_px() * BUBBLE_MAX_RATIO))

    def _bubble_menu(self, event, text: str) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="複製這則訊息", command=lambda: self._copy_block(text))
        menu.add_separator()
        menu.add_command(label="複製整段對話", command=self.app.copy_transcript)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _write_user_log(self, text: str) -> None:
        """Embed a read-only, horizontally scrollable terminal-output block."""
        self._write_scrollable_block(text.rstrip("\n"), "日誌", self._inline_log_widgets)

    def _write_agent_markdown(self, markdown: str, body_tag: str) -> None:
        """Render prose as Markdown, but give fenced code its own copy control."""
        position = 0
        for match in _FENCED_CODE_BLOCK.finditer(markdown):
            prose = markdown[position:match.start()].strip()
            if prose:
                richtext.insert(self.text, prose, (body_tag,))
            self._write_code_card(match.group(2).rstrip("\n"))
            position = match.end()
        tail = markdown[position:].strip()
        if tail:
            richtext.insert(self.text, tail, (body_tag,))

    def _write_scrollable_block(
        self, content: str, label: str, collection: list[tk.Frame]
    ) -> None:
        """Embed monospaced content with independent scrollbars and a copy button."""
        line_count = max(1, content.count("\n") + 1)
        frame = tk.Frame(self.text, bg=COLORS["border"], bd=0, highlightthickness=0)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        header = tk.Frame(frame, bg=COLORS["tool_bg"], bd=0, highlightthickness=0)
        tk.Label(
            header, text=label, bg=COLORS["tool_bg"], fg=COLORS["muted"],
            font=(self.app.ui_font, 8), anchor="w", padx=8,
        ).grid(row=0, column=0, sticky="w")
        tk.Button(
            header, text="複製", command=lambda value=content: self._copy_block(value),
            bg=COLORS["primary"], fg="white", activebackground=COLORS["primary_hover"],
            activeforeground="white", bd=0, padx=8, pady=1,
            font=(self.app.ui_font, 8), cursor="hand2",
        ).grid(row=0, column=1, padx=(0, 3), pady=2, sticky="w")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=1, pady=(1, 0))
        log = tk.Text(
            frame, width=96, height=min(12, max(4, line_count)), wrap="none", bd=0,
            padx=8, pady=6, bg=COLORS["tool_bg"], fg=COLORS["tool"],
            insertbackground=COLORS["tool"], font=(self.app.mono_font, 9),
            selectbackground=COLORS["select"], selectforeground=COLORS["text"],
        )
        vertical = ttk.Scrollbar(frame, orient="vertical", command=log.yview,
                                 style="VS.Vertical.TScrollbar")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=log.xview)
        log.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        log.grid(row=1, column=0, sticky="nsew", padx=1)
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal.grid(row=2, column=0, sticky="ew", padx=1, pady=(0, 1))
        log.insert("1.0", content)
        log.configure(state="disabled")
        frame.grid_propagate(False)
        frame.configure(
            height=header.winfo_reqheight() + log.winfo_reqheight()
            + horizontal.winfo_reqheight() + 3
        )
        collection.append(frame)
        self.text.window_create("end", window=frame, padx=BLOCK_PADX_PX, pady=4, align="top")
        self._resize_inline_blocks()
        self.text.insert("end", "\n", "user_log")

    def _write_code_card(self, content: str) -> None:
        """Embed a wrapping, no-scrollbar code card with a copy action."""
        frame = tk.Frame(self.text, bg=COLORS["border"], bd=0, highlightthickness=0)
        frame.columnconfigure(0, weight=1)
        header = tk.Frame(frame, bg=COLORS["tool_bg"], bd=0, highlightthickness=0)
        tk.Label(
            header, text="程式碼", bg=COLORS["tool_bg"], fg=COLORS["muted"],
            font=(self.app.ui_font, 8), anchor="w", padx=8,
        ).grid(row=0, column=0, sticky="w")
        tk.Button(
            header, text="複製", command=lambda value=content: self._copy_block(value),
            bg=COLORS["primary"], fg="white", activebackground=COLORS["primary_hover"],
            activeforeground="white", bd=0, padx=8, pady=1,
            font=(self.app.ui_font, 8), cursor="hand2",
        ).grid(row=0, column=1, padx=(0, 3), pady=2, sticky="w")
        header.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        code = tk.Label(
            frame, text=content, bg=COLORS["tool_bg"], fg=COLORS["tool"],
            font=(self.app.mono_font, 9), justify="left", anchor="nw", padx=8, pady=7,
        )
        code.grid(row=1, column=0, sticky="ew", padx=1, pady=(0, 1))
        frame.grid_propagate(False)
        self._inline_code_widgets.append(frame)
        self._inline_code_labels.append(code)
        self._bind_block_mousewheel(frame)
        self.text.window_create("end", window=frame, padx=BLOCK_PADX_PX, pady=4, align="top")
        self._resize_inline_blocks()
        self.text.insert("end", "\n", "agent")

    def _copy_block(self, content: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(content)
        self.update()
        self.app.set_status("已複製區塊內容")

    def _bind_block_mousewheel(self, widget: tk.Misc) -> None:
        """Let a code card pass wheel input through to the transcript."""
        widget.bind("<MouseWheel>", self._scroll_transcript_from_block, add="+")
        widget.bind("<Button-4>", lambda _event: self._scroll_transcript_units(-1), add="+")
        widget.bind("<Button-5>", lambda _event: self._scroll_transcript_units(1), add="+")
        for child in widget.winfo_children():
            self._bind_block_mousewheel(child)

    def _scroll_transcript_from_block(self, event) -> str:
        delta = getattr(event, "delta", 0)
        if delta:
            units = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)
            self._scroll_transcript_units(units)
        return "break"

    def _scroll_transcript_units(self, units: int) -> str:
        self.text.yview_scroll(units, "units")
        self._update_jump_button()
        return "break"

    def _resize_inline_blocks(self, _event=None) -> None:
        """Re-measure every embedded block against the transcript's width."""
        wraplength = self._bubble_wraplength()
        for label in getattr(self, "_inline_bubbles", ()):
            if label.winfo_exists():
                label.configure(wraplength=wraplength)
        # Fill the measure rather than shrink-wrapping the code, but stop at the
        # measure: sized to the pane instead, a card ran past the right inset on
        # a wide window and was clipped there.
        width = max(320, self._content_px() - BLOCK_PADX_PX - BLOCK_RMARGIN_PX)
        for frame in getattr(self, "_inline_log_widgets", ()):
            if frame.winfo_exists():
                frame.configure(width=width)
        for frame, code in zip(
            getattr(self, "_inline_code_widgets", ()),
            getattr(self, "_inline_code_labels", ()),
        ):
            if not frame.winfo_exists():
                continue
            frame.configure(width=width)
            code.configure(wraplength=max(200, width - 18))
            frame.update_idletasks()
            header = frame.winfo_children()[0]
            frame.configure(height=header.winfo_reqheight() + code.winfo_reqheight() + 2)

    def _write_tool(self, text: str, body_tag: str) -> None:
        """Tool calls belong to the right-side audit log, never the answer."""
        return

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
        self.text.tag_bind(tag, "<Button-1>", lambda e, t=tag: self._file_link_menu(e, self._link_targets[t]))
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

    def _markdown_link_menu(self, event):
        """Offer the file action only for a local Markdown link."""
        index = self.text.index(f"@{event.x},{event.y}")
        line_start, line_end = f"{index} linestart", f"{index} lineend"
        line = self.text.get(line_start, line_end)
        # richtext renders `[name](file:///C:/x)` as `name ⟨file:///C:/x⟩`.
        match = re.search(r"⟨([^⟩]+)⟩", line)
        target = match.group(1) if match else ""
        if not target:
            # A Markdown link whose label equals its URL has no muted target
            # appended, so read the text covered by the clicked link tag.
            ranges = self.text.tag_ranges("md_link")
            for start, end in zip(ranges[::2], ranges[1::2]):
                if self.text.compare(start, "<=", index) and self.text.compare(index, "<", end):
                    target = self.text.get(start, end)
                    break
        path = self._local_link_path(target)
        if path:
            self._file_link_menu(event, path)
            return "break"
        return None

    @staticmethod
    def _local_link_path(target: str) -> str | None:
        if target.startswith("file://"):
            parsed = urlparse(target)
            path = unquote(parsed.path)
            # file:///C:/... turns into /C:/... when parsed on Windows.
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
                path = path[1:]
            return path
        if re.match(r"^[A-Za-z]:[\\/]", target) or target.startswith("\\\\"):
            return target
        return None

    def _file_link_menu(self, event, path: str):
        """Context action for underlined local-file links."""
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="開啟", command=lambda: self._open_path(path))
        menu.add_command(label="複製完整路徑", command=lambda: self._copy_file_path(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _copy_file_path(self, path: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(path)
        self.app.set_status("已複製完整檔案路徑")

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
            prompt = (PDF_ONLY_PROMPT if any(path.lower().endswith(".pdf") for path in images)
                      else IMAGE_ONLY_PROMPT)
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

        ttk.Label(form, text="Claude 權限", style="TLabel").grid(row=6, column=0, sticky="w", pady=4)
        self.claude_permission_var = tk.StringVar()
        ttk.Combobox(
            form,
            textvariable=self.claude_permission_var,
            values=(INHERIT,) + tuple(codex_runner.CLAUDE_PERMISSION_LABELS.values()),
            state="readonly",
            width=46,
        ).grid(row=6, column=1, sticky="w", padx=(10, 0))
        self.claude_permission_hint = ttk.Label(form, text="", style="Muted.TLabel")
        self.claude_permission_hint.grid(row=7, column=1, sticky="w", padx=(10, 0))

        ttk.Label(form, text="提示詞", style="TLabel").grid(row=8, column=0, sticky="nw", pady=4)
        prompt_wrap = tk.Frame(form, bg=COLORS["border"])
        prompt_wrap.grid(row=8, column=1, sticky="ew", padx=(10, 0))
        self.prompt_text = tk.Text(
            prompt_wrap, height=5, wrap="word", bd=0, padx=8, pady=6,
            bg=COLORS["panel"], fg=COLORS["text"], font=(app.ui_font, 9), undo=True,
        )
        self.prompt_text.pack(fill="both", expand=True, padx=1, pady=1)
        self.prompt_hint = ttk.Label(form, text="", style="Muted.TLabel",
                                     wraplength=620, justify="left")
        self.prompt_hint.grid(row=9, column=1, sticky="w", padx=(10, 0), pady=(2, 0))

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
        self.claude_permission_var.set(
            codex_runner.claude_permission_label(project.get("claude_permission"))
            if project.get("claude_permission") else INHERIT
        )
        self.claude_permission_hint.configure(
            text="繼承值：" + codex_runner.claude_permission_label(
                ws.inherited(project["id"], "claude_permission")
            )
        )

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
        claude_permission = self.claude_permission_var.get()
        ws = self.app.ws
        ws.set_option(self.project_id, "cwd", cwd)
        ws.set_option(self.project_id, "model", self.model_var.get().strip())
        ws.set_option(self.project_id, "prompt", self.prompt_text.get("1.0", "end").strip())
        ws.set_option(
            self.project_id,
            "sandbox",
            "" if sandbox == INHERIT else codex_runner.sandbox_from_label(sandbox),
        )
        ws.set_option(
            self.project_id,
            "claude_permission",
            "" if claude_permission == INHERIT
            else codex_runner.claude_permission_from_label(claude_permission),
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


def _is_terminal_log(text: str) -> bool:
    """Whether a pasted user message looks like multi-line terminal output."""
    return text.count("\n") >= 2 and bool(_TERMINAL_LOG_LINE.search(text))


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
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    TreeAgentApp(root, home=args.home)
    root.mainloop()


if __name__ == "__main__":
    main()
