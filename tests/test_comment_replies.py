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
        self.claim_patch = patch("app.claim_comment_reply", return_value=True)
        self.mark_patch = patch("app.mark_comment_reply")
        self.claim_patch.start()
        self.mark_patch.start()

    def tearDown(self):
        self.claim_patch.stop()
        self.mark_patch.stop()

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

        count = agent.auto_reply_scan_for_service(
            self.service, self.channel_id, telegram_user_id="12345"
        )

        self.assertEqual(count, 0)
        self.assertNotIn("COMMENT_RETRY_1", agent.DONE)
        self.assertEqual(agent.COMMENT_PENDING_COUNTS.get(self.channel_id), 1)
        self.assertIsNotNone(agent.LAST_COMMENT_SCAN.get(self.channel_id))

    def test_first_scan_includes_unanswered_comments_older_than_seven_days(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        old_comment = {
            "id": "OLD_COMMENT_90D",
            "snippet": {"topLevelComment": {"id": "OLD_COMMENT_2D", "snippet": {
                "authorChannelId": {"value": "OTHER_CHANNEL"},
                "publishedAt": (now - timedelta(days=90)).isoformat(),
                "textOriginal": "Nice video!", "videoId": "VIDEO_OLD"
            }}}
        }
        self.service.commentThreads().list().execute.return_value = {
            "items": [old_comment]
        }
        comments = agent.list_recent_comments(self.service, self.channel_id)
        self.assertEqual([item["id"] for item in comments], ["OLD_COMMENT_2D"])


    @patch("app.send_tg")
    @patch("app.ai_reply")
    @patch("app.should_auto_reply", return_value=True)
    @patch("app.list_recent_comments")
    def test_one_gemini_failure_does_not_block_later_comments(
        self, list_comments, should_reply, make_reply, notify
    ):
        second = {
            "id": "COMMENT_RETRY_2",
            "video_id": "VIDEO_2",
            "title": "Second video",
            "text": "Great!",
        }
        agent.DONE.discard(second["id"])
        list_comments.return_value = [self.comment, second]
        make_reply.side_effect = [
            RuntimeError("Gemini HTTP 429"),
            "Thanks for watching!",
        ]

        count = agent.auto_reply_scan_for_service(
            self.service, self.channel_id, telegram_user_id="12345"
        )

        self.assertEqual(count, 1)
        self.assertNotIn(self.comment["id"], agent.DONE)
        self.assertIn(second["id"], agent.DONE)
        self.assertEqual(notify.call_count, 1)
        self.assertIsNotNone(agent.LAST_COMMENT_SCAN.get(self.channel_id))
        agent.DONE.discard(second["id"])


    @patch("app.claim_comment_reply", return_value=False)
    @patch("app.ai_reply")
    @patch("app.list_recent_comments")
    def test_persistently_claimed_comment_is_skipped_after_restart(
        self, list_comments, make_reply, claim
    ):
        list_comments.return_value = [self.comment]
        count = agent.auto_reply_scan_for_service(self.service, self.channel_id)
        self.assertEqual(count, 0)
        make_reply.assert_not_called()
        claim.assert_called_once_with(self.channel_id, "COMMENT_RETRY_1")

    @patch("app.mark_comment_reply")
    @patch("app.claim_comment_reply", return_value=True)
    @patch("app.send_tg")
    @patch("app.ai_reply", return_value="Thank you!")
    @patch("app.should_auto_reply", return_value=True)
    @patch("app.list_recent_comments")
    def test_success_persists_replied_state(
        self, list_comments, should_reply, make_reply, notify, claim, mark
    ):
        agent.DONE.discard("COMMENT_RETRY_1")
        list_comments.return_value = [self.comment]
        count = agent.auto_reply_scan_for_service(self.service, self.channel_id)
        self.assertEqual(count, 1)
        claim.assert_called_once_with(self.channel_id, "COMMENT_RETRY_1")
        mark.assert_called_once_with(self.channel_id, "COMMENT_RETRY_1", "replied")

    def test_scan_lock_prevents_overlapping_scans(self):
        self.assertTrue(agent.COMMENT_SCAN_LOCK.acquire(blocking=False))
        try:
            count = agent.auto_reply_scan_for_service(self.service, self.channel_id)
            self.assertEqual(count, 0)
        finally:
            agent.COMMENT_SCAN_LOCK.release()


if __name__ == "__main__":
    unittest.main()
