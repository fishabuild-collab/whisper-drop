"""WhisperDrop — drag-and-drop Cantonese/Chinese media transcriber."""

import queue
import threading
import time
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
BG = "#1e1e2e"
SURFACE = "#2a2a3e"
ACCENT = "#7c6af7"
TEXT = "#e0e0f0"
SUBTEXT = "#888aaa"
GREEN = "#4ade80"
RED = "#f87171"


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


# ── main window ──────────────────────────────────────────────────────────────
class WhisperDropApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("WhisperDrop")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(640, 520)

        self._queued_files: list[Path] = []
        self._job_queue: queue.Queue = queue.Queue()
        self._ui_queue: queue.Queue = queue.Queue()
        self._stop_flag = False
        self._running = False
        self._model = None
        self._loaded_model_name = None
        self._durations: list[float] = []

        self._build_ui()
        self.after(100, self._poll_ui_queue)

    # ── UI construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        pad = dict(padx=14, pady=6)

        # ── drop zone ────────────────────────────────────────────────────────
        self._drop_frame = tk.Frame(self, bg=SURFACE, bd=0, relief="flat",
                                    highlightthickness=2, highlightbackground=ACCENT)
        self._drop_frame.pack(fill="x", padx=16, pady=(16, 6))
        self._drop_label = tk.Label(
            self._drop_frame,
            text="Drop media files or a folder here",
            font=("SF Pro Display", 15),
            fg=SUBTEXT, bg=SURFACE, pady=28,
        )
        self._drop_label.pack(fill="x")
        for w in (self._drop_frame, self._drop_label):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self._on_drop)

        # ── controls row ─────────────────────────────────────────────────────
        ctrl = tk.Frame(self, bg=BG)
        ctrl.pack(fill="x", padx=16, pady=4)

        tk.Label(ctrl, text="Model:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")
        self._model_var = tk.StringVar(value=DEFAULT_MODEL)
        model_menu = ttk.Combobox(ctrl, textvariable=self._model_var,
                                   values=MODELS, state="readonly", width=8)
        model_menu.pack(side="left", padx=(4, 16))

        tk.Label(ctrl, text="Output:", fg=SUBTEXT, bg=BG,
                 font=("SF Pro Text", 12)).pack(side="left")
        self._output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        tk.Entry(ctrl, textvariable=self._output_var, width=32,
                 bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("SF Mono", 11)).pack(side="left", padx=(4, 6))
        tk.Button(ctrl, text="…", command=self._pick_output,
                  bg=SURFACE, fg=TEXT, relief="flat",
                  font=("SF Pro Text", 12), padx=6).pack(side="left")

        # ── ETA bar ──────────────────────────────────────────────────────────
        self._eta_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._eta_var, fg=ACCENT, bg=BG,
                 font=("SF Pro Text", 12)).pack(anchor="w", padx=16, pady=(4, 0))

        # ── file queue list ──────────────────────────────────────────────────
        list_frame = tk.Frame(self, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(4, 0))

        self._listbox = tk.Listbox(
            list_frame, bg=SURFACE, fg=TEXT, selectbackground=ACCENT,
            font=("SF Mono", 11), relief="flat", bd=0,
            activestyle="none", height=8,
        )
        scrollbar = tk.Scrollbar(list_frame, orient="vertical",
                                  command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=scrollbar.set)
        self._listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ── log ──────────────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(6, 6))

        self._log = tk.Text(
            log_frame, bg=SURFACE, fg=TEXT, font=("SF Mono", 10),
            relief="flat", bd=0, state="disabled", height=7,
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
        self._clear_btn = tk.Button(
            btn_frame, text="Clear queue", command=self._clear_queue,
            bg=SURFACE, fg=SUBTEXT, font=("SF Pro Text", 11),
            relief="flat", padx=12, pady=4, cursor="hand2",
        )
        self._clear_btn.pack(pady=(6, 0))

    # ── event handlers ───────────────────────────────────────────────────────
    def _on_drop(self, event):
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

    def _pick_output(self):
        folder = filedialog.askdirectory(initialdir=self._output_var.get())
        if folder:
            self._output_var.set(folder)

    def _clear_queue(self):
        if self._running:
            return
        self._queued_files.clear()
        self._listbox.delete(0, "end")
        self._eta_var.set("")

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
        self._run_btn.config(text="Cancel", bg=RED)

        for f in self._queued_files:
            self._job_queue.put(f)

        model_name = self._model_var.get()
        output_folder = Path(self._output_var.get())

        thread = threading.Thread(
            target=self._worker,
            args=(model_name, output_folder, list(self._queued_files)),
            daemon=True,
        )
        thread.start()

    def _worker(self, model_name: str, output_folder: Path, files: list[Path]):
        self._ui_put(("log", f"Loading model '{model_name}'…\n"))

        if self._loaded_model_name != model_name:
            self._model = transcribe.load_model(model_name)
            self._loaded_model_name = model_name

        self._ui_put(("log", f"Model ready. Processing {len(files)} file(s).\n\n"))

        transcribe.run_batch(
            model=self._model,
            files=files,
            output_folder=output_folder,
            language="zh",
            progress_callback=self._on_progress,
            stop_flag=lambda: self._stop_flag,
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
            self._update_list_item(idx, f"skip", f.name)
            self._log_append(f"[{i}/{n}] Skipped (already done): {f.name}\n")

        elif event == "start":
            self._update_list_item(idx, "processing", f.name)
            self._log_append(f"[{i}/{n}] Transcribing: {f.name}\n")

        elif event == "done":
            elapsed = data["elapsed"]
            self._durations.append(elapsed)
            self._update_list_item(idx, "done", f.name)
            self._log_append(f"  ✅ Done in {elapsed:.1f}s\n")
            self._update_eta(i, n)

        elif event == "error":
            self._update_list_item(idx, "error", f.name)
            self._log_append(f"  ❌ Error: {data['error']}\n")
            self._update_eta(i, n)

        elif event == "finish":
            s, fail, sk = data["succeeded"], data["failed"], data["skipped"]
            self._log_append(
                f"\n── Done ── {s} succeeded · {fail} failed · {sk} skipped\n"
                f"SRT files saved to: {self._output_var.get()}\n"
            )
            self._eta_var.set("")
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

    def _log_append(self, text: str):
        self._log.config(state="normal")
        self._log.insert("end", text)
        self._log.see("end")
        self._log.config(state="disabled")


if __name__ == "__main__":
    app = WhisperDropApp()
    app.mainloop()
