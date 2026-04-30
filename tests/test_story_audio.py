import asyncio
import io
import pathlib
import sys
import unittest
import wave
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.auphonic import AuphonicError, AuphonicResult
from app.core.elevenlabs import TTSResult
from app.core import story_audio
from app.core.story_audio import _add_breaks_to_paragraph, _format_text_for_tts, _parse_output_format, _pcm_chunks_to_wav


class StoryAudioTests(unittest.TestCase):
    def test_parse_output_format_reads_pcm_sample_rate(self):
        codec, sample_rate = _parse_output_format("pcm_24000")
        self.assertEqual(codec, "pcm")
        self.assertEqual(sample_rate, 24000)

    def test_pcm_chunks_to_wav_builds_single_valid_wave(self):
        audio = _pcm_chunks_to_wav([b"\x00\x00" * 10, b"\x01\x00" * 10], 24000)
        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 24000)
            self.assertEqual(wav_file.getnframes(), 2900)

    def test_ssml_sentence_breaks_are_not_added_at_chunk_end(self):
        ssml = _add_breaks_to_paragraph("Limitless. Already done.")

        self.assertEqual(ssml, "<speak>Limitless. Already done.</speak>")
        self.assertNotIn("<break", ssml)

    def test_ssml_sentence_breaks_are_not_added_after_story_sentence(self):
        ssml = _add_breaks_to_paragraph(
            "Fresh berries from the Santa Monica Farmers Market gleamed like jewels. "
            "I popped a strawberry into my mouth."
        )

        self.assertIn("gleamed like jewels. I popped", ssml)
        self.assertNotIn("jewels. <break", ssml)

    def test_tts_formatting_uses_larger_chunks_for_story_audio(self):
        text = " ".join(f"Sentence {idx}." for idx in range(1, 17))

        chunks = [p for p in _format_text_for_tts(text).split("\n\n") if p.strip()]

        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(chunk.count(".") == 8 for chunk in chunks))

    def test_tts_formatting_does_not_split_after_preposition(self):
        text = (
            "I stepped out onto the private balcony of the Pelican Hill Resort, "
            "where the Already Done team celebration was in full swing."
        )

        formatted = _format_text_for_tts(text)

        self.assertIn("celebration was in full swing.", formatted)
        self.assertNotIn("celebration was in.", formatted)

    def test_tts_formatting_does_not_leave_conjunction_dangling(self):
        text = (
            "I walked through the bright front door of my new home with sunlight across the floor "
            "and felt every room welcome me with warmth and peace as my dream settled around me."
        )

        formatted = _format_text_for_tts(text)

        self.assertNotIn("floor and.", formatted)
        self.assertIn("floor. and felt", formatted)

    def test_generate_story_audio_skips_auphonic_when_disabled(self):
        tts_result = TTSResult(
            audio_bytes=b"\x00\x00" * 20,
            content_type="application/octet-stream",
            output_format="pcm_24000",
            request_id="req-1",
            history_item_id="hist-1",
        )

        with (
            patch.object(story_audio.settings, "AUPHONIC_ENABLED", False),
            patch.object(story_audio.settings, "SUPABASE_URL", ""),
            patch.object(story_audio.settings, "SUPABASE_KEY", ""),
            patch.object(story_audio.settings, "ELEVENLABS_TTS_OUTPUT_FORMAT", "pcm_24000"),
            patch.object(story_audio, "text_to_speech", AsyncMock(return_value=tts_result)),
            patch.object(story_audio, "safe_partial_update", MagicMock()),
            patch.object(story_audio, "process_audio_with_auphonic", AsyncMock()) as auphonic_mock,
        ):
            result = asyncio.run(
                story_audio.generate_and_store_story_audio(
                    story_id=1,
                    voice_id="voice-1",
                    text="I felt calm and grateful. Already done.",
                )
            )

        self.assertEqual(result["content_type"], "audio/wav")
        self.assertFalse(result["postprocess"]["enabled"])
        self.assertFalse(result["postprocess"]["applied"])
        self.assertIsNone(result["postprocess"]["fallback_reason"])
        auphonic_mock.assert_not_awaited()

    def test_generate_story_audio_uses_auphonic_result_and_persists_metadata(self):
        tts_result = TTSResult(
            audio_bytes=b"\x00\x00" * 20,
            content_type="application/octet-stream",
            output_format="pcm_24000",
            request_id="req-1",
            history_item_id="hist-1",
        )
        auphonic_result = AuphonicResult(
            audio_bytes=b"ID3processed",
            content_type="audio/mpeg",
            file_ext="mp3",
            output_format="mp3",
            filename="story-2.mp3",
            metadata={
                "provider": "auphonic",
                "processing_ms": 321.0,
                "production_uuid": "prod-123",
                "status": 3,
                "status_string": "Done",
            },
        )
        safe_update = MagicMock()

        with (
            patch.object(story_audio.settings, "AUPHONIC_ENABLED", True),
            patch.object(story_audio.settings, "AUPHONIC_API_KEY", "api-key"),
            patch.object(story_audio.settings, "AUPHONIC_PRESET", "Already Done"),
            patch.object(story_audio.settings, "SUPABASE_URL", "https://supabase.test"),
            patch.object(story_audio.settings, "SUPABASE_KEY", "supabase-key"),
            patch.object(story_audio.settings, "ELEVENLABS_TTS_OUTPUT_FORMAT", "pcm_24000"),
            patch.object(story_audio, "text_to_speech", AsyncMock(return_value=tts_result)),
            patch.object(story_audio, "process_audio_with_auphonic", AsyncMock(return_value=auphonic_result)),
            patch.object(story_audio, "_upload_temp_file", return_value="https://cdn.test/audio.mp3"),
            patch.object(story_audio, "_store_manifest", return_value="https://cdn.test/manifest.json"),
            patch.object(story_audio, "safe_partial_update", safe_update),
        ):
            result = asyncio.run(
                story_audio.generate_and_store_story_audio(
                    story_id=2,
                    voice_id="voice-2",
                    text="I was already living in abundance. Everything worked out.",
                )
            )

        self.assertEqual(result["content_type"], "audio/mpeg")
        self.assertTrue(result["postprocess"]["applied"])
        self.assertEqual(result["postprocess"]["production_uuid"], "prod-123")
        self.assertEqual(result["metrics"]["postprocess_ms"], 321.0)
        final_update = safe_update.call_args_list[-1].kwargs["optional_payload"]
        self.assertEqual(final_update["audio_postprocess_status"], "completed")
        self.assertEqual(final_update["audio_postprocess_provider"], "auphonic")
        self.assertEqual(final_update["audio_postprocess_metadata"]["production_uuid"], "prod-123")

    def test_generate_story_audio_falls_back_when_auphonic_fails(self):
        tts_result = TTSResult(
            audio_bytes=b"\x00\x00" * 20,
            content_type="application/octet-stream",
            output_format="pcm_24000",
            request_id="req-1",
            history_item_id="hist-1",
        )

        with (
            patch.object(story_audio.settings, "AUPHONIC_ENABLED", True),
            patch.object(story_audio.settings, "AUPHONIC_API_KEY", "api-key"),
            patch.object(story_audio.settings, "AUPHONIC_PRESET", "Already Done"),
            patch.object(story_audio.settings, "SUPABASE_URL", ""),
            patch.object(story_audio.settings, "SUPABASE_KEY", ""),
            patch.object(story_audio.settings, "ELEVENLABS_TTS_OUTPUT_FORMAT", "pcm_24000"),
            patch.object(story_audio, "text_to_speech", AsyncMock(return_value=tts_result)),
            patch.object(
                story_audio,
                "process_audio_with_auphonic",
                AsyncMock(side_effect=AuphonicError("Auphonic timed out")),
            ),
            patch.object(story_audio, "safe_partial_update", MagicMock()),
        ):
            result = asyncio.run(
                story_audio.generate_and_store_story_audio(
                    story_id=3,
                    voice_id="voice-3",
                    text="My dream life had already unfolded beautifully.",
                )
            )

        self.assertEqual(result["content_type"], "audio/wav")
        self.assertFalse(result["postprocess"]["applied"])
        self.assertIn("Auphonic timed out", result["postprocess"]["fallback_reason"])
        self.assertIsNone(result["metrics"]["postprocess_ms"])


if __name__ == "__main__":
    unittest.main()
