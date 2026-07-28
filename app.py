"""WhisperDrop — drag-and-drop media transcriber."""

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

import transcribe

# ── constants ────────────────────────────────────────────────────────────────
MODELS = ["tiny", "base", "small", "medium", "large"]
DEFAULT_MODEL = "medium"
DEFAULT_OUTPUT = Path.home() / "Desktop" / "WhisperDrop" / "output"
STATUS_ICONS = {"waiting": "⏳", "processing": "🔄", "done": "✅", "error": "❌", "skip": "⏭️"}

# Warm, paper-like palette — no purple, no neon "tech brand" gradients.
BG = "#F5F4EE"          # cream page background
SURFACE = "#EEEBE2"     # slightly darker cream for panels/inputs
SURFACE_ALT = "#FFFFFF" # white for the drop zone, to pop off the cream
ACCENT = "#C15F3C"      # clay / terracotta — primary actions
ACCENT_HOVER = "#A94E2F"
TEXT = "#30291F"        # warm near-black
SUBTEXT = "#8A8171"     # muted warm grey
BORDER = "#DED8C7"      # soft tan border
GREEN = "#6E8F5C"       # muted sage — success accent
RED = "#96402A"         # deeper brick — errors/cancel
RED_HOVER = "#7C331F"
DISABLED = "#C9C2B2"    # muted tan — disabled button fill

LANG_LABELS = list(transcribe.LANGUAGES.values())
LANG_CODES = list(transcribe.LANGUAGES.keys())
DEFAULT_LANG_LABEL = transcribe.LANGUAGES[transcribe.DEFAULT_LANGUAGE]


# ── helpers ──────────────────────────────────────────────────────────────────
def parse_dropped(raw: str) -> list[Path]:
    """Parse tkinterdnd2's space-separated / brace-quoted path string."""
    paths, i = [], 0
    raw = raw.strip()
    while i < len(raw):
        if raw[i] == "{":
            end = raw.index("}", i)
            paths.append(Path(raw[i + 1:end]))
            i = end + 2
        else:
            end = raw.find(" ", i)
            if end == -1:
                paths.append(Path(raw[i:]))
                break
            paths.append(Path(raw[i:end]))
            i = end + 1
    return paths


def reveal_in_finder(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["open", str(folder)])
    else:
        subprocess.run(["xdg-open", str(folder)])


def _round_rect_points(x1, y1, x2, y2, r):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


class RoundedButton(tk.Canvas):
    """A flat, rounded-rectangle button drawn on a Canvas.

    Sidesteps Tk's native macOS button chrome (thick borders, system
    gradient fill that ignores `bg`) entirely by drawing everything itself.
    """

    def __init__(self, parent, text, command=None, radius=10, bg=ACCENT, fg="white",
                 hover_bg=None, disabled_bg=DISABLED, font=("SF Pro Text", 13),
                 padx=22, pady=9, width=None, height=None):
        self._bg = bg
        self._hover_bg = hover_bg or bg
        self._disabled_bg = disabled_bg
        self._fg = fg
        self._font = font
        self._command = command
        self._radius = radius
        self._text = text
        self._enabled = True
        self._hovering = False

        tmp = tk.Label(parent, text=text, font=font)
        tmp.update_idletasks()
        text_w = tmp.winfo_reqwidth()
        text_h = tmp.winfo_reqheight()
        tmp.destroy()

        self._w = width or (text_w + padx * 2)
        self._h = height or (text_h + pady * 2)

        parent_bg = parent["bg"] if "bg" in parent.keys() else BG
        super().__init__(parent, width=self._w, height=self._h,
                          bg=parent_bg, highlightthickness=0, bd=0)

        self._redraw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _redraw(self):
        self.delete("all")
        if not self._enabled:
            fill = self._disabled_bg
        elif self._hovering:
            fill = self._hover_bg
        else:
            fill = self._bg
        self.create_polygon(
            _round_rect_points(1, 1, self._w - 1, self._h - 1, self._radius),
            smooth=True, fill=fill, outline=fill,
        )
        self.create_text(self._w / 2, self._h / 2, text=self._text,
                          fill=self._fg, font=self._font)

    def _on_enter(self, _e):
        if self._enabled:
            self._hovering = True
            self.configure(cursor="hand2")
            self._redraw()

    def _on_leave(self, _e):
        self._hovering = False
        self._redraw()

    def _on_click(self, _e):
        if self._enabled and self._command:
            self._command()

    def set_text(self, text: str):
        self._text = text
        self._redraw()

    def set_bg(self, bg: str, hover_bg: str = None):
        self._bg = bg
        self._hover_bg = hover_bg or bg
        self._redraw()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._redraw()


class RoundedDropZone(tk.Canvas):
    """Rounded-rectangle drop target with an embedded label."""

    def __init__(self, parent, text, radius=16, height=110):
        parent_bg = parent["bg"] if "bg" in parent.keys() else BG
        super().__init__(parent, height=height, bg=parent_bg, highlightthickness=0, bd=0)
        self._radius = radius
        self._default_text = text
        self._border_color = BORDER
        self._label = tk.Label(self, text=text, font=("SF Pro Display", 15),
                                fg=SUBTEXT, bg=SURFACE_ALT)
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, _e):
        self._redraw()

    def _redraw(self):
        self.delete("shape")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        self.create_polygon(
            _round_rect_points(1, 1, w - 1, h - 1, self._radius),
            smooth=True, fill=SURFACE_ALT, outline=self._border_color, width=2, tags="shape",
        )
        self.tag_lower("shape")
        self._label.place(relx=0.5, rely=0.5, anchor="center")

    def set_hover(self, hovering: bool):
        self._border_color = ACCENT if hovering else BORDER
        self._label.configure(
            text="Release to add files" if hovering else self._default_text,
            fg=ACCENT if hovering else SUBTEXT,
        )
        self._redraw()

    def register_drop_targets(self, on_drop, on_enter, on_leave):
        for w in (self, self._label):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", on_drop)
            w.dnd_bind("<<DropEnter>>", on_enter)
            w.dnd_bind("<<DropLeave>>", on_leave)


# ── main window ──────────────────────────────────────────────────────────────
class WhisperDropApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("WhisperDrop")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(680, 620)

        self._queued_files: list[Path] = []
        self._job_queue: queue.Queue = queue.Queue()
        self._ui_queue: queue.Queue = queue.Queue()
        self._stop_flag = False
        self._running = False
        self._model = None
        self._loaded_model_name = None
        self._durations: list[float] = []
        self._failed_files: list[Path] = []

        self._init_style()
        self._build_ui()
        self.after(100, self._poll_ui_queue)

    # ── ttk styling ───────────────────────────────────────────────────────────
    def _init_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("Warm.Horizontal.TScale", background=BG, troughcolor=SURFACE,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        style.map("Warm.Horizontal.TScale", background=[("active", BG)])

        style.configure("Overall.Horizontal.TProgressbar", troughcolor=SURFACE,
                        background=ACCENT, bordercolor=SURFACE, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("File.Horizontal.TProgressbar", troughcolor=SURFACE,
                        background=GREEN, bordercolor=SURFACE, lightcolor=GREEN, darkcolor=GREEN)

        style.configure("Warm.TCombobox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, arrowcolor=SUBTEXT, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, relief="flat")
        style.map("Warm.TCombobox",
                  fieldbackground=[("readonly", SURFACE), ("disabled", SURFACE)],
                  foreground=[("readonly", TEXT), ("disabled", SUBTEXT)],
                  selectbackground=[("readonly", SURFACE)],
                  selectforeground=[("readonly", TEXT)],
                  background=[("readonly", SURFACE)])
        # Combobox dropdown list colors are controlled via option db, not style.
        self.option_add("*TCombobox*Listbox.background", SURFACE)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "white")

    # ── UI construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        # ── drop zone ────────────────────────────────────────────────────────
        self._drop_zone = RoundedDropZone(self, "Drop media files or a folder here")
        self._drop_zone.pack(fill="x", padx=16, pady=(16, 6))
        self._drop_zone.register_drop_targets(self._on_drop, self._on_drag_enter, self._on_drag_leave)

        # ── controls row 1: model + language ────────────────────────────────
        ctrl1 = tk.Frame(self, bg=BG)
        ctrl1.pack(fill="x", padx=16, pady=(4, 0))

        tk.Label(ctrl1, text="Model:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")
        self._model_var = tk.StringVar(value=DEFAULT_MODEL)
        ttk.Combobox(ctrl1, textvariable=self._model_var, values=MODELS,
                     state="readonly", width=8, style="Warm.TCombobox").pack(side="left", padx=(4, 16))

        tk.Label(ctrl1, text="Language:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")
        self._lang_var = tk.StringVar(value=DEFAULT_LANG_LABEL)
        ttk.Combobox(ctrl1, textvariable=self._lang_var, values=LANG_LABELS,
                     state="readonly", width=22, style="Warm.TCombobox").pack(side="left", padx=(4, 16))

        self._translate_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            ctrl1, text="Translate to English", variable=self._translate_var,
            fg=TEXT, bg=BG, selectcolor=SURFACE, activebackground=BG,
            activeforeground=TEXT, font=("SF Pro Text", 12),
            highlightthickness=0, bd=0,
        ).pack(side="left")

        # ── controls row 2: output folder ───────────────────────────────────
        ctrl2 = tk.Frame(self, bg=BG)
        ctrl2.pack(fill="x", padx=16, pady=(10, 4))

        tk.Label(ctrl2, text="Output:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")
        self._output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        tk.Entry(ctrl2, textvariable=self._output_var, width=30,
                 bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("SF Mono", 11),
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side="left", padx=(4, 8), ipady=4)
        RoundedButton(ctrl2, "…", command=self._pick_output, bg=SURFACE, fg=TEXT,
                      hover_bg=BORDER, width=34, height=30, radius=8,
                      font=("SF Pro Text", 12)).pack(side="left", padx=(0, 6))
        RoundedButton(ctrl2, "Open output folder", command=self._open_output,
                      bg=SURFACE, fg=TEXT, hover_bg=BORDER, radius=8,
                      font=("SF Pro Text", 11), padx=14, height=30).pack(side="left")

        # ── controls row 3: characters per line ─────────────────────────────
        ctrl3 = tk.Frame(self, bg=BG)
        ctrl3.pack(fill="x", padx=16, pady=(10, 4))

        tk.Label(ctrl3, text="Characters per line:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")

        self._max_chars_var = tk.IntVar(value=transcribe.DEFAULT_MAX_CHARS)

        self._max_chars_scale = ttk.Scale(
            ctrl3, from_=transcribe.MIN_MAX_CHARS, to=transcribe.MAX_MAX_CHARS,
            orient="horizontal", style="Warm.Horizontal.TScale", length=180,
            command=self._on_scale_change,
        )
        self._max_chars_scale.set(transcribe.DEFAULT_MAX_CHARS)
        self._max_chars_scale.pack(side="left", padx=(8, 10))

        self._max_chars_entry = tk.Entry(
            ctrl3, textvariable=self._max_chars_var, width=4,
            bg=SURFACE, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=("SF Mono", 11), justify="center",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._max_chars_entry.pack(side="left", ipady=3)
        self._max_chars_entry.bind("<Return>", self._on_entry_change)
        self._max_chars_entry.bind("<FocusOut>", self._on_entry_change)

        # ── overall progress ─────────────────────────────────────────────────
        self._eta_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._eta_var, fg=ACCENT, bg=BG,
                 font=("SF Pro Text", 12)).pack(anchor="w", padx=16, pady=(10, 2))

        self._overall_bar = ttk.Progressbar(
            self, style="Overall.Horizontal.TProgressbar",
            orient="horizontal", mode="determinate", maximum=100,
        )
        self._overall_bar.pack(fill="x", padx=16, pady=(0, 6))

        # ── current file progress ───────────────────────────────────────────
        self._file_progress_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._file_progress_var, fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 11)).pack(anchor="w", padx=16)

        self._file_bar = ttk.Progressbar(
            self, style="File.Horizontal.TProgressbar",
            orient="horizontal", mode="determinate", maximum=100,
        )
        self._file_bar.pack(fill="x", padx=16, pady=(0, 8))

        # ── file queue list ──────────────────────────────────────────────────
        list_frame = tk.Frame(self, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 0))

        self._listbox = tk.Listbox(
            list_frame, bg=SURFACE, fg=TEXT, selectbackground=ACCENT,
            selectforeground="white", font=("SF Mono", 11), relief="flat", bd=0,
            activestyle="none", height=7,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        scrollbar = tk.Scrollbar(list_frame, orient="vertical",
                                  command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=scrollbar.set)
        self._listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ── reorder / remove controls ───────────────────────────────────────
        order_frame = tk.Frame(list_frame, bg=BG)
        order_frame.pack(side="left", fill="y", padx=(8, 0))
        for label, cmd in (
            ("↑", self._move_up), ("↓", self._move_down), ("✕", self._remove_selected),
        ):
            RoundedButton(order_frame, label, command=cmd, bg=SURFACE, fg=TEXT,
                          hover_bg=BORDER, width=32, height=30, radius=8,
                          font=("SF Pro Text", 12)).pack(pady=3)

        # ── log ──────────────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(10, 6))

        self._log = tk.Text(
            log_frame, bg=SURFACE, fg=TEXT, font=("SF Mono", 10),
            relief="flat", bd=0, state="disabled", height=6,
            wrap="word", highlightthickness=1, highlightbackground=BORDER,
        )
        log_scroll = tk.Scrollbar(log_frame, orient="vertical", command=self._log.yview)
        self._log.configure(yscrollcommand=log_scroll.set)
        self._log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        # ── run / cancel button ──────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=(4, 16))
        self._run_btn = RoundedButton(
            btn_frame, "Run", command=self._toggle_run,
            bg=ACCENT, hover_bg=ACCENT_HOVER, fg="white",
            font=("SF Pro Text", 14, "bold"), radius=12, padx=36, pady=10,
        )
        self._run_btn.pack()
        bottom_row = tk.Frame(btn_frame, bg=BG)
        bottom_row.pack(pady=(8, 0))
        RoundedButton(bottom_row, "Clear queue", command=self._clear_queue,
                      bg=SURFACE, fg=SUBTEXT, hover_bg=BORDER, radius=8,
                      font=("SF Pro Text", 11), padx=14, height=28).pack(side="left", padx=4)
        RoundedButton(bottom_row, "Retry failed", command=self._retry_failed,
                      bg=SURFACE, fg=SUBTEXT, hover_bg=BORDER, radius=8,
                      font=("SF Pro Text", 11), padx=14, height=28).pack(side="left", padx=4)

    # ── event handlers ───────────────────────────────────────────────────────
    def _on_drop(self, event):
        self._on_drag_leave(event)
        dropped = parse_dropped(event.data)
        new_files = transcribe.find_media_files(dropped)
        already = set(self._queued_files)
        added = [f for f in new_files if f not in already]
        self._queued_files.extend(added)
        for f in added:
            self._listbox.insert("end", f"{STATUS_ICONS['waiting']}  {f.name}")
        self._log_append(f"Added {len(added)} file(s) to queue.\n")
        if self._running:
            for f in added:
                self._job_queue.put(f)

    def _on_drag_enter(self, event):
        self._drop_zone.set_hover(True)

    def _on_drag_leave(self, event):
        self._drop_zone.set_hover(False)

    def _on_scale_change(self, value):
        self._max_chars_var.set(round(float(value)))

    def _on_entry_change(self, event):
        try:
            v = int(self._max_chars_var.get())
        except (ValueError, tk.TclError):
            v = transcribe.DEFAULT_MAX_CHARS
        v = max(transcribe.MIN_MAX_CHARS, min(transcribe.MAX_MAX_CHARS, v))
        self._max_chars_var.set(v)
        self._max_chars_scale.set(v)

    def _pick_output(self):
        folder = filedialog.askdirectory(initialdir=self._output_var.get())
        if folder:
            self._output_var.set(folder)

    def _open_output(self):
        reveal_in_finder(Path(self._output_var.get()))

    def _clear_queue(self):
        if self._running:
            return
        self._queued_files.clear()
        self._listbox.delete(0, "end")
        self._eta_var.set("")
        self._reset_progress_bars()

    def _selected_index(self):
        sel = self._listbox.curselection()
        return sel[0] if sel else None

    def _move_up(self):
        if self._running:
            return
        i = self._selected_index()
        if i is None or i == 0:
            return
        self._queued_files[i - 1], self._queued_files[i] = self._queued_files[i], self._queued_files[i - 1]
        self._refresh_listbox()
        self._listbox.selection_set(i - 1)

    def _move_down(self):
        if self._running:
            return
        i = self._selected_index()
        if i is None or i >= len(self._queued_files) - 1:
            return
        self._queued_files[i + 1], self._queued_files[i] = self._queued_files[i], self._queued_files[i + 1]
        self._refresh_listbox()
        self._listbox.selection_set(i + 1)

    def _remove_selected(self):
        if self._running:
            return
        i = self._selected_index()
        if i is None:
            return
        del self._queued_files[i]
        self._refresh_listbox()

    def _retry_failed(self):
        if self._running or not self._failed_files:
            return
        for f in self._failed_files:
            if f not in self._queued_files:
                self._queued_files.append(f)
        self._failed_files = []
        self._refresh_listbox()

    def _refresh_listbox(self):
        self._listbox.delete(0, "end")
        for f in self._queued_files:
            self._listbox.insert("end", f"{STATUS_ICONS['waiting']}  {f.name}")

    def _toggle_run(self):
        if self._running:
            self._stop_flag = True
            self._run_btn.set_text("Stopping…")
            self._run_btn.set_enabled(False)
        else:
            if not self._queued_files:
                self._log_append("No files queued. Drop some files first.\n")
                return
            self._start_run()

    # ── run logic ────────────────────────────────────────────────────────────
    def _start_run(self):
        self._stop_flag = False
        self._running = True
        self._durations.clear()
        self._failed_files = []
        self._reset_progress_bars()
        self._run_btn.set_text("Cancel")
        self._run_btn.set_bg(RED, RED_HOVER)

        self._on_entry_change(None)  # normalize/clamp any typed value before using it

        model_name = self._model_var.get()
        output_folder = Path(self._output_var.get())
        lang_code = LANG_CODES[LANG_LABELS.index(self._lang_var.get())]
        task = "translate" if self._translate_var.get() else "transcribe"
        max_chars = self._max_chars_var.get()

        thread = threading.Thread(
            target=self._worker,
            args=(model_name, output_folder, list(self._queued_files), lang_code, task, max_chars),
            daemon=True,
        )
        thread.start()

    def _worker(self, model_name, output_folder, files, lang_code, task, max_chars):
        self._ui_put(("log", f"Loading model '{model_name}'…\n"))

        if self._loaded_model_name != model_name:
            self._model = transcribe.load_model(model_name)
            self._loaded_model_name = model_name

        self._ui_put(("log", f"Model ready. Processing {len(files)} file(s).\n\n"))

        transcribe.run_batch(
            model=self._model,
            files=files,
            output_folder=output_folder,
            language=lang_code,
            progress_callback=self._on_progress,
            stop_flag=lambda: self._stop_flag,
            task=task,
            max_chars=max_chars,
        )

    def _on_progress(self, event: str, data: dict):
        self._ui_put((event, data))

    # ── UI queue polling ─────────────────────────────────────────────────────
    def _poll_ui_queue(self):
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                self._handle_ui_msg(msg)
        except queue.Empty:
            pass
        self.after(80, self._poll_ui_queue)

    def _ui_put(self, msg):
        self._ui_queue.put(msg)

    def _handle_ui_msg(self, msg):
        event, data = msg

        if event == "log":
            self._log_append(data)
            return

        i, n, f = data["index"], data["total"], data["file"]
        idx = self._queued_files.index(f) if f in self._queued_files else -1

        if event == "skip":
            self._update_list_item(idx, "skip", f.name)
            self._log_append(f"[{i}/{n}] Skipped (already done): {f.name}\n")
            self._overall_bar["value"] = (i / n) * 100

        elif event == "start":
            self._update_list_item(idx, "processing", f.name)
            self._log_append(f"[{i}/{n}] Transcribing: {f.name}\n")
            self._file_bar["value"] = 0
            self._file_progress_var.set(f"{f.name} — 0%")

        elif event == "progress":
            pct = int(data["fraction"] * 100)
            self._file_bar["value"] = pct
            self._file_progress_var.set(f"{f.name} — {pct}%")

        elif event == "done":
            elapsed = data["elapsed"]
            self._durations.append(elapsed)
            self._update_list_item(idx, "done", f.name)
            self._log_append(f"  ✅ Done in {elapsed:.1f}s\n")
            self._file_bar["value"] = 100
            self._file_progress_var.set(f"{f.name} — 100%")
            self._overall_bar["value"] = (i / n) * 100
            self._update_eta(i, n)

        elif event == "error":
            self._update_list_item(idx, "error", f.name)
            self._log_append(f"  ❌ Error: {data['error']}\n")
            self._failed_files.append(f)
            self._overall_bar["value"] = (i / n) * 100
            self._update_eta(i, n)

        elif event == "finish":
            s, fail, sk = data["succeeded"], data["failed"], data["skipped"]
            self._log_append(
                f"\n── Done ── {s} succeeded · {fail} failed · {sk} skipped\n"
                f"SRT files saved to: {self._output_var.get()}\n"
            )
            self._eta_var.set("")
            self._file_progress_var.set("")
            self._running = False
            self._stop_flag = False
            self._run_btn.set_text("Run")
            self._run_btn.set_bg(ACCENT, ACCENT_HOVER)
            self._run_btn.set_enabled(True)

    def _update_list_item(self, idx: int, status: str, name: str):
        if idx < 0:
            return
        self._listbox.delete(idx)
        self._listbox.insert(idx, f"{STATUS_ICONS[status]}  {name}")

    def _update_eta(self, completed: int, total: int):
        remaining = total - completed
        if remaining <= 0 or not self._durations:
            self._eta_var.set("")
            return
        avg = sum(self._durations) / len(self._durations)
        secs = avg * remaining
        if secs >= 60:
            eta_str = f"~{int(secs // 60)}m {int(secs % 60)}s"
        else:
            eta_str = f"~{int(secs)}s"
        self._eta_var.set(f"Processing {completed}/{total} — Est. remaining: {eta_str}")

    def _reset_progress_bars(self):
        self._overall_bar["value"] = 0
        self._file_bar["value"] = 0
        self._file_progress_var.set("")

    def _log_append(self, text: str):
        self._log.config(state="normal")
        self._log.insert("end", text)
        self._log.see("end")
        self._log.config(state="disabled")


if __name__ == "__main__":
    app = WhisperDropApp()
    app.mainloop()
