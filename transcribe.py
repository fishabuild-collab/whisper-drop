"""Whisper transcription logic — imported by app.py."""

import io
import re
import subprocess
import time
from pathlib import Path

# Ensure a bundled ffmpeg is on PATH before whisper loads (teammates may not
# have Homebrew's ffmpeg). static-ffmpeg ships a static binary per platform and
# is installed by the bootstrap launcher. No-op if unavailable.
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

import whisper

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus",
}

# Common languages Whisper supports well, shown in the dropdown.
# "auto" lets Whisper detect the language from the audio itself.
LANGUAGES = {
    "auto": "Auto-detect",
    "zh": "Chinese (incl. Cantonese)",
    "en": "English",
    "yue": "Cantonese (yue)",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "hi": "Hindi",
    "pt": "Portuguese",
}
DEFAULT_LANGUAGE = "zh"
DEFAULT_MAX_CHARS = 42
MIN_MAX_CHARS = 20
MAX_MAX_CHARS = 80
_BLOCK_MAX_DURATION = 5.0  # seconds, not user-configurable — keeps blocks readable

_TS_RE = re.compile(r"\[([\d:.]+)\s*-->\s*([\d:.]+)\]")


def find_media_files(paths: list[Path]) -> list[Path]:
    """Expand a mixed list of files and folders into individual media files."""
    result = []
    for p in paths:
        if p.is_dir():
            result.extend(
                f for f in sorted(p.rglob("*"))
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
            )
        elif p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            result.append(p)
    seen = set()
    deduped = []
    for f in result:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped


def seconds_to_srt_timestamp(seconds: float) -> str:
    ms = int((seconds % 1) * 1000)
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        start = seconds_to_srt_timestamp(seg["start"])
        end = seconds_to_srt_timestamp(seg["end"])
        text = seg["text"].strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _flatten_words(segments) -> list[dict]:
    words = []
    for seg in segments:
        words.extend(seg.get("words") or [])
    return words


# CJK ranges where words/characters run together with no space: Chinese
# (incl. Cantonese) ideographs, Japanese kana, Hangul, and CJK punctuation.
_CJK_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF),   # CJK Unified Ideographs (+ Ext A)
    (0x3040, 0x30FF),                      # Hiragana, Katakana
    (0xAC00, 0xD7A3),                      # Hangul syllables
    (0x3000, 0x303F), (0xFF00, 0xFFEF),   # CJK/fullwidth punctuation
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _smart_join(parts: list[str]) -> str:
    """Join word fragments, omitting the space around any CJK boundary
    (Chinese/Cantonese/Japanese/Korean don't separate words with spaces)
    while keeping spaces between Latin-script words."""
    text = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not text or _is_cjk(text[-1]) or _is_cjk(p[0]):
            text += p
        else:
            text += " " + p
    return text


def words_to_blocks(
    words: list[dict],
    max_chars: int = DEFAULT_MAX_CHARS,
    max_duration: float = _BLOCK_MAX_DURATION,
) -> list[tuple[float, float, str]]:
    """Group word-level timestamps into short subtitle blocks."""
    blocks = []
    cur_words: list[dict] = []
    cur_start = None

    for w in words:
        text = w["word"].strip()
        if not text:
            continue
        if cur_start is None:
            cur_start = w["start"]

        candidate = _smart_join([x["word"] for x in cur_words] + [text])
        too_long = len(candidate) > max_chars
        too_slow = (w["end"] - cur_start) > max_duration
        ends_sentence = cur_words and cur_words[-1]["word"].strip()[-1:] in ".!?。！？"

        if cur_words and (too_long or too_slow or ends_sentence):
            blocks.append((cur_start, cur_words[-1]["end"],
                            _smart_join([x["word"] for x in cur_words])))
            cur_words, cur_start = [], w["start"]

        cur_words.append(w)

    if cur_words:
        blocks.append((cur_start, cur_words[-1]["end"],
                        _smart_join([x["word"] for x in cur_words])))
    return blocks


def blocks_to_srt(blocks: list[tuple[float, float, str]]) -> str:
    lines = []
    for i, (start, end, text) in enumerate(blocks, start=1):
        lines.append(f"{i}\n{seconds_to_srt_timestamp(start)} --> {seconds_to_srt_timestamp(end)}\n{text}\n")
    return "\n".join(lines)


def load_model(model_name: str):
    return whisper.load_model(model_name)


def get_duration(media_path: Path) -> float | None:
    """Media duration in seconds via ffprobe, or None if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(media_path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def _parse_timestamp(ts: str) -> float:
    parts = [float(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


class _ProgressCapture(io.TextIOBase):
    """Intercepts Whisper's verbose stdout lines to report decode progress."""

    def __init__(self, duration: float | None, on_fraction):
        self._duration = duration
        self._on_fraction = on_fraction
        self._buffer = ""

    def write(self, s):
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            m = _TS_RE.search(line)
            if m and self._duration:
                end = _parse_timestamp(m.group(2))
                self._on_fraction(min(end / self._duration, 1.0))
        return len(s)

    def flush(self):
        pass


def transcribe_file(
    model,
    media_path: Path,
    language: str = DEFAULT_LANGUAGE,
    task: str = "transcribe",
    on_fraction=None,
) -> dict:
    """Returns Whisper's raw result dict (segments include word-level timestamps)."""
    lang_arg = None if language == "auto" else language
    duration = get_duration(media_path) if on_fraction else None

    kwargs = dict(language=lang_arg, task=task, fp16=False, verbose=True, word_timestamps=True)

    def _run():
        return model.transcribe(str(media_path), **kwargs)

    import contextlib

    if on_fraction and duration:
        capture = _ProgressCapture(duration, on_fraction)
        try:
            with contextlib.redirect_stdout(capture):
                result = _run()
        except (ValueError, IndexError):
            kwargs.pop("language", None)
            with contextlib.redirect_stdout(capture):
                result = _run()
        on_fraction(1.0)
    else:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                result = _run()
            except (ValueError, IndexError):
                kwargs.pop("language", None)
                result = _run()

    return result


def _write_srt_and_txt(result: dict, max_chars: int, srt_path: Path, txt_path: Path):
    words = _flatten_words(result["segments"])
    if words:
        content = blocks_to_srt(words_to_blocks(words, max_chars=max_chars))
    else:
        content = segments_to_srt(result["segments"])
    srt_path.write_text(content, encoding="utf-8")
    txt_path.write_text(content, encoding="utf-8")


def run_batch(
    model,
    files: list[Path],
    output_folder: Path,
    language: str,
    progress_callback,
    stop_flag,
    task: str = "transcribe",
    max_chars: int = DEFAULT_MAX_CHARS,
    also_english: bool = False,
):
    """
    Transcribe a list of files.
    If also_english is True and language isn't already English, a second
    English SRT/TXT pair is written alongside the primary one, suffixed
    "<name>.en.srt" / "<name>.en.txt".

    progress_callback(event, data) receives:
      ("skip",     {"index": i, "total": n, "file": Path})
      ("start",    {"index": i, "total": n, "file": Path})
      ("progress", {"index": i, "total": n, "file": Path, "fraction": float})
      ("done",     {"index": i, "total": n, "file": Path, "elapsed": float})
      ("error",    {"index": i, "total": n, "file": Path, "error": str})
      ("finish",   {"succeeded": int, "failed": int, "skipped": int})
    """
    output_folder.mkdir(parents=True, exist_ok=True)
    total = len(files)
    succeeded = failed = skipped = 0

    for i, media_path in enumerate(files, start=1):
        if stop_flag():
            break

        srt_path = output_folder / (media_path.stem + ".srt")
        txt_path = output_folder / (media_path.stem + ".txt")
        needs_english = also_english and language != "en"
        en_srt_path = output_folder / (media_path.stem + ".en.srt")
        en_txt_path = output_folder / (media_path.stem + ".en.txt")

        if srt_path.exists() and (not needs_english or en_srt_path.exists()):
            skipped += 1
            progress_callback("skip", {"index": i, "total": total, "file": media_path})
            continue

        progress_callback("start", {"index": i, "total": total, "file": media_path})
        t0 = time.time()
        try:
            primary_scale = 0.5 if needs_english else 1.0

            def on_fraction(frac, _i=i, _f=media_path, _scale=primary_scale):
                progress_callback("progress", {"index": _i, "total": total, "file": _f, "fraction": frac * _scale})

            result = transcribe_file(model, media_path, language, task, on_fraction)
            _write_srt_and_txt(result, max_chars, srt_path, txt_path)

            if needs_english:
                def on_fraction_en(frac, _i=i, _f=media_path):
                    progress_callback("progress", {"index": _i, "total": total, "file": _f, "fraction": 0.5 + frac * 0.5})

                en_result = transcribe_file(model, media_path, language, "translate", on_fraction_en)
                _write_srt_and_txt(en_result, max_chars, en_srt_path, en_txt_path)

            elapsed = time.time() - t0
            succeeded += 1
            progress_callback("done", {"index": i, "total": total, "file": media_path, "elapsed": elapsed})
        except Exception as exc:
            failed += 1
            progress_callback("error", {"index": i, "total": total, "file": media_path, "error": str(exc)})

    progress_callback("finish", {"succeeded": succeeded, "failed": failed, "skipped": skipped})
