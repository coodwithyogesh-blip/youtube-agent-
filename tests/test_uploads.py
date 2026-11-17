import unittest
from datetime import datetime, timezone
from upload_utils import parse_tags, parse_publish_at, build_video_metadata

class UploadUtilityTests(unittest.TestCase):
    def test_parse_tags_accepts_json_or_comma_list(self):
        self.assertEqual(parse_tags('["Free Fire", "Gaming"]'), ["Free Fire", "Gaming"])
        self.assertEqual(parse_tags("Free Fire, Gaming"), ["Free Fire", "Gaming"])

    def test_rejects_more_than_500_characters_per_tag(self):
        with self.assertRaises(ValueError):
            parse_tags("x" * 501)

    def test_scheduled_time_is_converted_from_india_timezone(self):
        result = parse_publish_at("2030-01-01T12:00", now=datetime(2029, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result, "2030-01-01T06:30:00Z")

    def test_rejects_past_schedule(self):
        with self.assertRaises(ValueError):
            parse_publish_at("2020-01-01T12:00", now=datetime(2029, 1, 1, tzinfo=timezone.utc))

    def test_metadata_enforces_youtube_title_limit(self):
        with self.assertRaises(ValueError):
            build_video_metadata("x" * 101, "description", "[]", "public", None)

    def test_scheduled_upload_is_private_until_publish_time(self):
        result = build_video_metadata("Title", "Description", "Gaming", "public",
                                      "2030-01-01T12:00:00Z")
        self.assertEqual(result["status"]["privacyStatus"], "private")
        self.assertEqual(result["status"]["publishAt"], "2030-01-01T12:00:00Z")

if __name__ == "__main__":
    unittest.main()
