import unittest

from youtube_insights import comment_poll_interval, score_video_seo, weak_seo_videos, seo_score_distribution


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
            "tags": ["Free Fire", "gameplay", "ranked", "tips", "mobile gaming"],
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


    def test_weak_seo_videos_returns_every_video_below_threshold(self):
        videos = [{"id": "a", "score": 45}, {"id": "b", "score": 90},
                  {"id": "c", "score": 70}]
        self.assertEqual([v["id"] for v in weak_seo_videos(videos)], ["a", "c"])

    def test_seo_score_distribution_counts_all_bands(self):
        videos = [{"score": 20}, {"score": 55}, {"score": 65},
                  {"score": 85}, {"score": 100}]
        self.assertEqual(seo_score_distribution(videos),
                         {"poor": 1, "fair": 2, "good": 1, "excellent": 1})


if __name__ == "__main__":
    unittest.main()
