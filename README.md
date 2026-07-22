# WhisperDrop

Drag-and-drop transcription for Mac. Drop audio/video files onto the window and get `.srt` subtitle files back — powered by OpenAI Whisper, running fully offline on your machine.

## How to use (for teammates)

1. **Copy the whole `WhisperDrop` folder** to your Mac (AirDrop, USB, or download).
2. **Right-click** `WhisperDrop.command` → **Open** → click **Open** in the dialog.
   *(You only need the right-click the first time — after that you can double-click.)*
3. **First launch takes a while.** It downloads everything it needs (~2GB, one time only). Leave it running until the app window appears.
4. **Drop media files or a folder** onto the window, pick a model size, and hit **Run**.

That's it — no Terminal commands, no installing Python, no Homebrew.

## Notes

- **First run needs internet.** After setup, it works completely offline.
- **Everything stays inside this folder.** The Python environment lives in `.venv/`, tools in `bin/`. Delete the folder to remove it all cleanly — nothing is installed system-wide.
- **Model sizes:** `medium` (default) is a good balance for Cantonese/Chinese. `large` is more accurate but slower; `small`/`base` are faster but rougher.
- **Language:** defaults to Chinese (`zh`), which handles Cantonese audio well.
- **Output:** `.srt` files go to the `output/` folder by default (changeable in the app).

## What's in the folder

| File | Purpose |
|------|---------|
| `WhisperDrop.command` | Double-click to launch (handles all setup) |
| `app.py` | The drag-and-drop GUI |
| `transcribe.py` | Whisper transcription logic |
| `requirements.txt` | Dependency list |
| `.venv/`, `bin/` | Auto-created on first run (not shared/committed) |
