"""Whisper transcription logic — imported by app.py."""

from pathlib import Path
import time
import whisper

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus",
}


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


def load_model(model_name: str):
    return whisper.load_model(model_name)


def transcribe_file(model, media_path: Path, language: str = "zh") -> list[dict]:
    try:
        result = model.transcribe(str(media_path), language=language, fp16=False, verbose=False)
    except (ValueError, IndexError):
        result = model.transcribe(str(media_path), fp16=False, verbose=False)
    return result["segments"]


def run_batch(
    model,
    files: list[Path],
    output_folder: Path,
    language: str,
    progress_callback,
    stop_flag,
):
    """
    Transcribe a list of files.
    progress_callback(event, data) receives:
      ("skip",   {"index": i, "total": n, "file": Path})
      ("start",  {"index": i, "total": n, "file": Path})
      ("done",   {"index": i, "total": n, "file": Path, "elapsed": float})
      ("error",  {"index": i, "total": n, "file": Path, "error": str})
      ("finish", {"succeeded": int, "failed": int, "skipped": int})
    """
    output_folder.mkdir(parents=True, exist_ok=True)
    total = len(files)
    succeeded = failed = skipped = 0

    for i, media_path in enumerate(files, start=1):
        if stop_flag():
            break

        srt_path = output_folder / (media_path.stem + ".srt")

        if srt_path.exists():
            skipped += 1
            progress_callback("skip", {"index": i, "total": total, "file": media_path})
            continue

        progress_callback("start", {"index": i, "total": total, "file": media_path})
        t0 = time.time()
        try:
            segments = transcribe_file(model, media_path, language)
            srt_path.write_text(segments_to_srt(segments), encoding="utf-8")
            elapsed = time.time() - t0
            succeeded += 1
            progress_callback("done", {"index": i, "total": total, "file": media_path, "elapsed": elapsed})
        except Exception as exc:
            failed += 1
            progress_callback("error", {"index": i, "total": total, "file": media_path, "error": str(exc)})

    progress_callback("finish", {"succeeded": succeeded, "failed": failed, "skipped": skipped})
