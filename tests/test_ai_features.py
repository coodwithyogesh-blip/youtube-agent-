import unittest
from unittest.mock import Mock, patch
import requests
from ai_utils import clean_generated_metadata, should_auto_reply, generate_content_with_fallback

class AIFeatureTests(unittest.TestCase):
    def test_metadata_has_title_description_tags_and_hashtags(self):
        raw = {"title":"  Free Fire clutch  ","description":"  Watch this!  ",
               "tags":["Free Fire","gaming"],"hashtags":["#FreeFire","#Gaming"]}
        result = clean_generated_metadata(raw)
        self.assertEqual(result["title"], "Free Fire clutch")
        self.assertEqual(result["description"], "Watch this!\n\n#FreeFire #Gaming")
        self.assertEqual(result["tags"], ["Free Fire", "gaming"])

    def test_metadata_rejects_missing_title(self):
        with self.assertRaises(ValueError):
            clean_generated_metadata({"title":"","description":"x","tags":[],"hashtags":[]})


    @patch("ai_utils.time.sleep")
    @patch("ai_utils.requests.post")
    def test_retries_transient_503_then_succeeds(self, post, sleep):
        failed = Mock(status_code=503)
        failed.raise_for_status.side_effect = requests.HTTPError("unavailable", response=failed)
        succeeded = Mock(status_code=200)
        succeeded.raise_for_status.return_value = None
        succeeded.json.return_value = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        post.side_effect = [failed, succeeded]

        result = generate_content_with_fallback(
            "secret", "gemini-3.8-flash", {"contents": []},
            fallback_model="gemini-3.5-flash", max_attempts=3
        )

        self.assertEqual(result["candidates"][0]["content"]["parts"][0]["text"], "ok")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("ai_utils.time.sleep")
    @patch("ai_utils.requests.post")
    def test_uses_fallback_model_after_primary_is_unavailable(self, post, sleep):
        failed = Mock(status_code=503)
        failed.raise_for_status.side_effect = requests.HTTPError("unavailable", response=failed)
        succeeded = Mock(status_code=200)
        succeeded.raise_for_status.return_value = None
        succeeded.json.return_value = {"fallback": True}
        post.side_effect = [failed, failed, succeeded]

        result = generate_content_with_fallback(
            "secret", "gemini-3.8-flash", {"contents": []},
            fallback_model="gemini-3.5-flash", max_attempts=2
        )

        self.assertEqual(result, {"fallback": True})
        self.assertIn("/models/gemini-3.5-flash:generateContent", post.call_args.args[0])

    @patch("ai_utils.requests.post")
    def test_does_not_retry_non_transient_errors_or_leak_api_key(self, post):
        failed = Mock(status_code=403)
        failed.json.return_value = {"error": {"message": "permission denied"}}
        failed.raise_for_status.side_effect = requests.HTTPError("forbidden", response=failed)
        post.return_value = failed

        with self.assertRaises(RuntimeError) as caught:
            generate_content_with_fallback(
                "secret-value", "gemini-3.8-flash", {"contents": []},
                fallback_model="gemini-3.5-flash"
            )

        self.assertIn("403", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertEqual(post.call_count, 1)

    def test_auto_reply_skips_obvious_spam_and_sensitive_requests(self):
        self.assertFalse(should_auto_reply("Buy followers now https://spam.example"))
        self.assertFalse(should_auto_reply("Please share your phone number and address"))
        self.assertTrue(should_auto_reply("Nice gameplay bro!"))

if __name__ == "__main__":
    unittest.main()
