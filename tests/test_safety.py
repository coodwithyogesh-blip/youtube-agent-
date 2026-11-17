import unittest
from app import telegram_html, comment_has_owner_reply

class SafetyTests(unittest.TestCase):
    def test_telegram_html_escapes_untrusted_text(self):
        self.assertEqual(telegram_html('<b>x & y</b>'), '&lt;b&gt;x &amp; y&lt;/b&gt;')

    def test_detects_reply_from_connected_channel(self):
        thread = {"replies":{"comments":[{"snippet":{"authorChannelId":{"value":"UC_OWNER"}}}]}}
        self.assertTrue(comment_has_owner_reply(thread, "UC_OWNER"))

    def test_does_not_treat_viewer_reply_as_owner_reply(self):
        thread = {"replies":{"comments":[{"snippet":{"authorChannelId":{"value":"UC_VIEWER"}}}]}}
        self.assertFalse(comment_has_owner_reply(thread, "UC_OWNER"))

if __name__ == "__main__":
    unittest.main()
