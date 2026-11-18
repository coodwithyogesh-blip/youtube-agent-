import unittest
from unittest.mock import Mock, patch

import app as agent


class CommentReplyRetryTests(unittest.TestCase):
    def setUp(self):
        self.channel_id = "UC_TEST_RETRY"
        agent.LAST_COMMENT_SCAN.pop(self.channel_id, None)
        agent.DONE.discard("COMMENT_RETRY_1")
        self.comment = {
            "id": "COMMENT_RETRY_1",
            "video_id": "VIDEO_1",
            "title": "Test video",
            "text": "Nice video!",
        }
        self.service = Mock()

    @patch("app.send_tg")
    @patch("app.ai_reply", return_value="Thank you!")
    @patch("app.should_auto_reply", return_value=True)
    @patch("app.list_recent_comments")
    def test_successful_reply_notifies_telegram_and_marks_scan_complete(
        self, list_comments, should_reply, make_reply, notify
    ):
        list_comments.return_value = [self.comment]

        count = agent.auto_reply_scan_for_service(
            self.service, self.channel_id, telegram_user_id="12345"
        )

        self.assertEqual(count, 1)
        self.assertIn("COMMENT_RETRY_1", agent.DONE)
        self.assertIsNotNone(agent.LAST_COMMENT_SCAN.get(self.channel_id))
        notify.assert_called_once()
        self.assertEqual(str(notify.call_args.args[0]), "12345")
        self.assertIn("Replied to your comment successfully", notify.call_args.args[1])

    @patch("app.ai_reply", side_effect=RuntimeError("Gemini HTTP 429"))
    @patch("app.should_auto_reply", return_value=True)
    @patch("app.list_recent_comments")
    def test_failed_ai_reply_does_not_advance_scan_cursor_or_mark_done(
        self, list_comments, should_reply, make_reply
    ):
        list_comments.return_value = [self.comment]

        with self.assertRaisesRegex(RuntimeError, "429"):
            agent.auto_reply_scan_for_service(
                self.service, self.channel_id, telegram_user_id="12345"
            )

        self.assertNotIn("COMMENT_RETRY_1", agent.DONE)
        self.assertNotIn(self.channel_id, agent.LAST_COMMENT_SCAN)


if __name__ == "__main__":
    unittest.main()
