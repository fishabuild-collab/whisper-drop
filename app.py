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
RED = "#96402A"         # deeper brick — errors/cancel (same warm family, clearly darker than ACCENT)

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


# ── main window ──────────────────────────────────────────────────────────────
class WhisperDropApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("WhisperDrop")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(680, 600)

        self._queued_files: list[Path] = []
        self._job_queue: queue.Queue = queue.Queue()
        self._ui_queue: queue.Queue = queue.Queue()
        self._stop_flag = False
        self._running = False
        self._model = None
        self._loaded_model_name = None
        self._durations: list[float] = []
        self._failed_files: list[Path] = []

        self._build_ui()
        self.after(100, self._poll_ui_queue)

    # ── UI construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        # ── drop zone ────────────────────────────────────────────────────────
        self._drop_frame = tk.Frame(self, bg=SURFACE_ALT, bd=0, relief="flat",
                                    highlightthickness=2, highlightbackground=BORDER)
        self._drop_frame.pack(fill="x", padx=16, pady=(16, 6))
        self._drop_label = tk.Label(
            self._drop_frame,
            text="Drop media files or a folder here",
            font=("SF Pro Display", 15),
            fg=SUBTEXT, bg=SURFACE_ALT, pady=28,
        )
        self._drop_label.pack(fill="x")
        for w in (self._drop_frame, self._drop_label):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self._on_drop)
            w.dnd_bind("<<DropEnter>>", self._on_drag_enter)
            w.dnd_bind("<<DropLeave>>", self._on_drag_leave)

        # ── controls row 1: model + language ────────────────────────────────
        ctrl1 = tk.Frame(self, bg=BG)
        ctrl1.pack(fill="x", padx=16, pady=(4, 0))

        tk.Label(ctrl1, text="Model:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")
        self._model_var = tk.StringVar(value=DEFAULT_MODEL)
        ttk.Combobox(ctrl1, textvariable=self._model_var, values=MODELS,
                     state="readonly", width=8).pack(side="left", padx=(4, 16))

        tk.Label(ctrl1, text="Language:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")
        self._lang_var = tk.StringVar(value=DEFAULT_LANG_LABEL)
        ttk.Combobox(ctrl1, textvariable=self._lang_var, values=LANG_LABELS,
                     state="readonly", width=22).pack(side="left", padx=(4, 16))

        self._translate_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            ctrl1, text="Translate to English", variable=self._translate_var,
            fg=TEXT, bg=BG, selectcolor=SURFACE, activebackground=BG,
            activeforeground=TEXT, font=("SF Pro Text", 12),
        ).pack(side="left")

        # ── controls row 2: output folder ───────────────────────────────────
        ctrl2 = tk.Frame(self, bg=BG)
        ctrl2.pack(fill="x", padx=16, pady=(6, 4))

        tk.Label(ctrl2, text="Output:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")
        self._output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        tk.Entry(ctrl2, textvariable=self._output_var, width=32,
                 bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("SF Mono", 11)).pack(side="left", padx=(4, 6))
        tk.Button(ctrl2, text="…", command=self._pick_output,
                  bg=SURFACE, fg=TEXT, relief="flat",
                  font=("SF Pro Text", 12), padx=6).pack(side="left", padx=(0, 6))
        tk.Button(ctrl2, text="Open output folder", command=self._open_output,
                  bg=SURFACE, fg=TEXT, relief="flat",
                  font=("SF Pro Text", 11), padx=8).pack(side="left")

        # ── controls row 3: characters per line ─────────────────────────────
        ctrl3 = tk.Frame(self, bg=BG)
        ctrl3.pack(fill="x", padx=16, pady=(2, 4))

        tk.Label(ctrl3, text="Characters per line:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")

        self._max_chars_var = tk.IntVar(value=transcribe.DEFAULT_MAX_CHARS)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Warm.Horizontal.TScale", background=BG, troughcolor=SURFACE)

        self._max_chars_scale = ttk.Scale(
            ctrl3, from_=transcribe.MIN_MAX_CHARS, to=transcribe.MAX_MAX_CHARS,
            orient="horizontal", style="Warm.Horizontal.TScale", length=180,
            command=self._on_scale_change,
        )
        self._max_chars_scale.set(transcribe.DEFAULT_MAX_CHARS)
        self._max_chars_scale.pack(side="left", padx=(6, 8))

        self._max_chars_entry = tk.Entry(
            ctrl3, textvariable=self._max_chars_var, width=4,
            bg=SURFACE, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=("SF Mono", 11), justify="center",
        )
        self._max_chars_entry.pack(side="left")
        self._max_chars_entry.bind("<Return>", self._on_entry_change)
        self._max_chars_entry.bind("<FocusOut>", self._on_entry_change)

        # ── overall progress ─────────────────────────────────────────────────
        self._eta_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._eta_var, fg=ACCENT, bg=BG,
                 font=("SF Pro Text", 12)).pack(anchor="w", padx=16, pady=(8, 2))

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Overall.Horizontal.TProgressbar", troughcolor=SURFACE,
                        background=ACCENT, bordercolor=SURFACE, lightcolor=ACCENT, darkcolor=ACCENT)
        self._overall_bar = ttk.Progressbar(
            self, style="Overall.Horizontal.TProgressbar",
            orient="horizontal", mode="determinate", maximum=100,
        )
        self._overall_bar.pack(fill="x", padx=16, pady=(0, 6))

        # ── current file progress ───────────────────────────────────────────
        self._file_progress_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._file_progress_var, fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 11)).pack(anchor="w", padx=16)

        style.configure("File.Horizontal.TProgressbar", troughcolor=SURFACE,
                        background=GREEN, bordercolor=SURFACE, lightcolor=GREEN, darkcolor=GREEN)
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
            font=("SF Mono", 11), relief="flat", bd=0,
            activestyle="none", height=7,
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
            tk.Button(order_frame, text=label, command=cmd,
                      bg=SURFACE, fg=TEXT, relief="flat",
                      font=("SF Pro Text", 12), width=2).pack(pady=2)

        # ── log ──────────────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(6, 6))

        self._log = tk.Text(
            log_frame, bg=SURFACE, fg=TEXT, font=("SF Mono", 10),
            relief="flat", bd=0, state="disabled", height=6,
            wrap="word",
        )
        log_scroll = tk.Scrollbar(log_frame, orient="vertical", command=self._log.yview)
        self._log.configure(yscrollcommand=log_scroll.set)
        self._log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        # ── run / cancel button ──────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=(0, 14))
        self._run_btn = tk.Button(
            btn_frame, text="Run", command=self._toggle_run,
            bg=ACCENT, fg="white", font=("SF Pro Text", 14, "bold"),
            relief="flat", padx=32, pady=8, cursor="hand2",
        )
        self._run_btn.pack()
        bottom_row = tk.Frame(btn_frame, bg=BG)
        bottom_row.pack(pady=(6, 0))
        self._clear_btn = tk.Button(
            bottom_row, text="Clear queue", command=self._clear_queue,
            bg=SURFACE, fg=SUBTEXT, font=("SF Pro Text", 11),
            relief="flat", padx=12, pady=4, cursor="hand2",
        )
        self._clear_btn.pack(side="left", padx=4)
        self._retry_btn = tk.Button(
            bottom_row, text="Retry failed", command=self._retry_failed,
            bg=SURFACE, fg=SUBTEXT, font=("SF Pro Text", 11),
            relief="flat", padx=12, pady=4, cursor="hand2",
        )
        self._retry_btn.pack(side="left", padx=4)

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
        self._drop_frame.configure(highlightbackground=ACCENT)
        self._drop_label.configure(text="Release to add files", fg=ACCENT)

    def _on_drag_leave(self, event):
        self._drop_frame.configure(highlightbackground=BORDER)
        self._drop_label.configure(text="Drop media files or a folder here", fg=SUBTEXT)

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
            self._run_btn.config(text="Stopping…", state="disabled")
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
        self._run_btn.config(text="Cancel", bg=RED)

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
            self._run_btn.config(text="Run", bg=ACCENT, state="normal")

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
