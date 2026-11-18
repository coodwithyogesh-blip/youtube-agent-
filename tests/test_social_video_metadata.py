import unittest
from unittest.mock import patch

from ai_utils import clean_generated_metadata, social_video_prompt


class SocialVideoMetadataTests(unittest.TestCase):
    def test_prompt_is_platform_specific_and_analyses_audio_visuals(self):
        prompt = social_video_prompt("instagram", "creator note")
        self.assertIn("Instagram", prompt)
        self.assertIn("visual", prompt.lower())
        self.assertIn("audio", prompt.lower())
        self.assertIn("creator note", prompt)

    def test_prompt_rejects_youtube_as_social_platform(self):
        with self.assertRaises(ValueError):
            social_video_prompt("youtube", "")

    def test_metadata_is_cleaned_for_upload(self):
        result = clean_generated_metadata({
            "title": "A video", "description": "A real clip",
            "tags": ["one", "one"], "hashtags": ["#One", "#Two"]
        })
        self.assertEqual(result["title"], "A video")
        self.assertEqual(result["tags"], ["one"])
        self.assertEqual(result["hashtags"], ["#One", "#Two"])


if __name__ == "__main__":
    unittest.main()
