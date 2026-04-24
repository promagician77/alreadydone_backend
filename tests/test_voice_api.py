import io
import pathlib
import sys
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import voice as voice_api
from app.main import app


class _UploadStub:
    def __init__(self, filename: str, content_type: str):
        self.filename = filename
        self.content_type = content_type


def _wav_bytes(duration_seconds: float = 1.0, sample_rate: int = 16000, sample_value: int = 16) -> bytes:
    frame_count = int(duration_seconds * sample_rate)
    frame = int(sample_value).to_bytes(2, byteorder="little", signed=True)
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(frame * frame_count)
        return buffer.getvalue()


class VoiceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_clone_rejects_short_audio(self):
        short_audio = _wav_bytes(duration_seconds=1.0)
        with patch("app.api.voice._get_audio_duration_seconds", return_value=1.0):
            response = self.client.post(
                "/api/voice/clone",
                data={"user_id": "1", "name": "Chris"},
                files={"files": ("sample.wav", short_audio, "audio/wav")},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("too short", response.json()["detail"])

    def test_generate_audio_maps_provider_errors(self):
        async_error = httpx.RequestError("boom", request=httpx.Request("POST", "https://api.elevenlabs.io/test"))
        with patch("app.api.voice.generate_and_store_story_audio", new=AsyncMock(side_effect=async_error)):
            response = self.client.post(
                "/api/voice/generate_audio",
                json={"voice_id": "voice_123", "story_id": 42, "model_id": "eleven_multilingual_v2"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Voice service error")

    def test_normalize_clone_audio_applies_gain_to_quiet_input(self):
        quiet_wav = _wav_bytes(duration_seconds=1.0, sample_value=1)
        louder_wav = _wav_bytes(duration_seconds=1.0, sample_value=500)

        def fake_run(command, stdout=None, stderr=None, check=False):
            output_path = command[-1]
            wav_bytes = louder_wav if "-af" in command else quiet_wav
            with open(output_path, "wb") as handle:
                handle.write(wav_bytes)
            return SimpleNamespace(returncode=0, stderr=b"")

        with (
            patch("app.api.voice.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("app.api.voice.subprocess.run", side_effect=fake_run),
            patch.object(voice_api.settings, "VOICE_CLONE_MIN_RMS_RATIO", 0.005),
            patch.object(voice_api.settings, "VOICE_CLONE_AUTO_GAIN_QUIET_AUDIO", True),
        ):
            normalized_name, normalized_bytes, normalized_type, meta = voice_api._normalize_clone_audio(
                "sample.m4a",
                b"fake-input",
            )

        self.assertEqual(normalized_name, "sample.wav")
        self.assertEqual(normalized_type, "audio/wav")
        self.assertEqual(normalized_bytes, louder_wav)
        self.assertTrue(meta["normalized"])
        self.assertTrue(meta["auto_gain_applied"])
        self.assertGreater(meta["post_gain_rms_ratio"], meta["pre_gain_rms_ratio"])

    def test_prepare_clone_file_rejects_very_quiet_audio_with_helpful_message(self):
        upload = _UploadStub("quiet.m4a", "audio/m4a")
        wav_bytes = _wav_bytes(duration_seconds=4.0, sample_value=1)

        with (
            patch("app.api.voice._normalize_clone_audio", return_value=("quiet.wav", wav_bytes, "audio/wav", {"normalized": True})),
            patch("app.api.voice._get_audio_duration_seconds", return_value=4.0),
            patch("app.api.voice._analyze_wav_bytes", return_value={"rms_ratio": 0.004, "peak_ratio": 0.1}),
            patch.object(voice_api.settings, "VOICE_CLONE_MIN_RMS_RATIO", 0.005),
        ):
            with self.assertRaises(HTTPException) as ctx:
                voice_api._prepare_clone_file(upload, b"fake-content")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("too quiet or silent", ctx.exception.detail)
        self.assertIn("record closer to the mic", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
