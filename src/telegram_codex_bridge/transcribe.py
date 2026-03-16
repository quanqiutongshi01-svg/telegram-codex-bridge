from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import tempfile


class WhisperTranscriber:
    def __init__(self, ffmpeg_binary: str, model_name: str, language: str | None = None):
        self.ffmpeg_binary = ffmpeg_binary
        self.model_name = model_name
        self.language = language
        self._model = None

    def _load_model(self):
        if self._model is None:
            import whisper

            self._model = whisper.load_model(self.model_name)
        return self._model

    def _normalize_with_ffmpeg(self, source: Path) -> Path:
        normalized_dir = Path(tempfile.mkdtemp(prefix="telegram-codex-voice-"))
        target = normalized_dir / f"{source.stem}.wav"
        subprocess.run(
            [
                self.ffmpeg_binary,
                "-y",
                "-i",
                str(source),
                "-ar",
                "16000",
                "-ac",
                "1",
                str(target),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return target

    def _transcribe_sync(self, source: Path) -> str:
        model = self._load_model()
        normalized = self._normalize_with_ffmpeg(source)
        result = model.transcribe(str(normalized), language=self.language)
        text = (result.get("text") or "").strip()
        if not text:
            raise RuntimeError("Whisper returned empty transcription")
        return text

    async def transcribe(self, source: Path) -> str:
        return await asyncio.to_thread(self._transcribe_sync, source)
