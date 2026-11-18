import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_utils import (
    clean_generated_metadata, generate_content_with_fallback, social_video_prompt
)


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

    @patch("ai_utils.time.sleep")
    @patch("ai_utils.requests.post")
    def test_primary_503_retries_then_uses_fallback_model(self, post, sleep):
        post.side_effect = [
            SimpleNamespace(status_code=503, json=lambda: {}),
            SimpleNamespace(status_code=503, json=lambda: {}),
            SimpleNamespace(status_code=503, json=lambda: {}),
            SimpleNamespace(status_code=503, json=lambda: {}),
            SimpleNamespace(status_code=200, json=lambda: {"ok": True}),
        ]

        result = generate_content_with_fallback(
            "test-key", "gemini-primary", {"contents": []},
            fallback_model="gemini-3.5-flash-lite", max_attempts=4
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(post.call_count, 5)
        self.assertIn("gemini-3.5-flash-lite", post.call_args.args[0])
        self.assertEqual(sleep.call_count, 3)

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
