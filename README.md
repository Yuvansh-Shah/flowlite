# flowlite

A local speech-to-text dictation tool for Windows. It lives in the system tray, transcribes your voice on the GPU, cleans up the text, and pastes it wherever the cursor is. No cloud, no API keys.

## What it does

flowlite is a push-to-talk dictation app — a local alternative to cloud dictation services. Hold a hotkey (or toggle hands-free), speak, and the text appears in whatever field you're focused on. Audio is captured locally and transcribed with faster-whisper (large-v3-turbo) on the GPU, with voice-activity detection to trim silence. The raw transcript is then cleaned by a local Ollama model that fixes punctuation and capitalization and removes filler words without rewriting your meaning, guarded by a word-count check so it can't run away with the text.

The UI is a draggable floating "pill" with a tray icon. Transcription runs on a worker thread so the interface stays responsive, and the paste pipeline detects when no text field is focused, keeps a short transcript queue so nothing is lost, and can re-paste the last result.

## Built with

- Python
- faster-whisper (CTranslate2) for transcription
- Ollama (gemma3:1b) for cleanup
- PyQt6 for the tray/pill UI
- sounddevice, keyboard, pyperclip

## Running it

```
pip install -r requirements.txt
python daemon.py
```

Or run `install.ps1`, which pulls the Ollama model, caches the Whisper model, and adds Startup/Desktop/Start-Menu shortcuts. Everything is configurable in `config.toml` — model, device, language, hotkeys, and UI options.

One implementation note: faster-whisper/CTranslate2 has to initialize its CUDA context before PyQt6 starts, and the NVIDIA cuBLAS/cuDNN DLLs are added to the search path, to avoid a Windows crash on startup.
