import io
import pathlib
import sys
import unittest
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.story_audio import _parse_output_format, _pcm_chunks_to_wav


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
            self.assertEqual(wav_file.getnframes(), 20)


if __name__ == "__main__":
    unittest.main()
