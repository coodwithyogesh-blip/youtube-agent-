import unittest
from ai_utils import clean_generated_metadata, should_auto_reply

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

    def test_auto_reply_skips_obvious_spam_and_sensitive_requests(self):
        self.assertFalse(should_auto_reply("Buy followers now https://spam.example"))
        self.assertFalse(should_auto_reply("Please share your phone number and address"))
        self.assertTrue(should_auto_reply("Nice gameplay bro!"))

if __name__ == "__main__":
    unittest.main()
