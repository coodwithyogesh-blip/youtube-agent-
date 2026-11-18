import unittest

from social_drafts import build_social_draft


class SocialDraftTests(unittest.TestCase):
    def test_instagram_draft_contains_topic_and_platform_tags(self):
        draft = build_social_draft("instagram", "new gaming video")
        self.assertEqual(draft["platform"], "instagram")
        self.assertIn("new gaming video", draft["caption"])
        self.assertIn("#Instagram", draft["hashtags"])

    def test_facebook_draft_has_platform_specific_call_to_action(self):
        draft = build_social_draft("facebook", "shop opening")
        self.assertIn("shop opening", draft["caption"])
        self.assertIn("Facebook", draft["caption"])

    def test_snapchat_draft_is_supported(self):
        draft = build_social_draft("snapchat", "new gaming clip")
        self.assertEqual(draft["platform"], "snapchat")
        self.assertIn("#Snapchat", draft["hashtags"])

    def test_rejects_empty_topic(self):
        with self.assertRaises(ValueError):
            build_social_draft("instagram", "   ")


if __name__ == "__main__":
    unittest.main()
