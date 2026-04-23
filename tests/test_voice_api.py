import io
import pathlib
import sys
import unittest
import wave
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app


def _wav_bytes(duration_seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    frame_count = int(duration_seconds * sample_rate)
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(b"\x10\x00" * frame_count)
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


if __name__ == "__main__":
    unittest.main()
