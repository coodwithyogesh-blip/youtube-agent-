import unittest

from youtube_insights import comment_poll_interval, score_video_seo


class YouTubeInsightsTests(unittest.TestCase):
    def test_comment_poll_interval_defaults_to_two_minutes(self):
        self.assertEqual(comment_poll_interval(""), 120)

    def test_comment_poll_interval_uses_two_minutes_minimum(self):
        self.assertEqual(comment_poll_interval("30"), 120)

    def test_comment_poll_interval_accepts_larger_interval(self):
        self.assertEqual(comment_poll_interval("300"), 300)

    def test_video_seo_score_rewards_complete_metadata(self):
        video = {
            "title": "Free Fire ranked gameplay tips",
            "description": "Watch this gameplay and subscribe for more tips. This video covers ranked gameplay, useful tactics, and practical tips for players.",
            "tags": ["Free Fire", "gameplay", "ranked"],
            "thumbnail": True,
        }
        result = score_video_seo(video)
        self.assertGreaterEqual(result["score"], 80)
        self.assertEqual(result["checks"]["thumbnail"], True)

    def test_video_seo_score_flags_missing_fields(self):
        result = score_video_seo({"title": "", "description": "", "tags": [], "thumbnail": False})
        self.assertLess(result["score"], 50)
        self.assertFalse(result["checks"]["title"])
        self.assertFalse(result["checks"]["description"])


if __name__ == "__main__":
    unittest.main()
