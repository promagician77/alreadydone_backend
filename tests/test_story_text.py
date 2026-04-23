import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.story_text import ensure_complete_story_text, prepare_story_for_narration, trim_to_sentence_boundary


class StoryTextTests(unittest.TestCase):
    def test_trim_to_sentence_boundary_avoids_mid_sentence_cut(self):
        text = "I walked into the room. The light felt warm and soft. Then I saw the table covered"
        trimmed, was_trimmed = trim_to_sentence_boundary(text, 65)
        self.assertTrue(was_trimmed)
        self.assertEqual(trimmed, "I walked into the room. The light felt warm and soft.")

    def test_prepare_story_for_narration_removes_bracketed_sound_cues(self):
        cleaned = prepare_story_for_narration("I smiled. [cat meow] The room felt calm.")
        self.assertEqual(cleaned, "I smiled. The room felt calm.")

    def test_ensure_complete_story_text_rejects_clipped_story(self):
        with self.assertRaises(ValueError):
            ensure_complete_story_text("I walked into the room and")


if __name__ == "__main__":
    unittest.main()
