import unittest
import os
from unittest.mock import patch
from cryptography.fernet import Fernet

from app import (callback_response, main_menu_keyboard, main_menu_text, format_youtube_accounts,
                 make_youtube_oauth_state, read_youtube_oauth_state,
                 encrypt_refresh_token, OAUTH_STATES)


class TelegramMenuTests(unittest.TestCase):
    def test_main_menu_has_all_requested_sections(self):
        rows = main_menu_keyboard()["inline_keyboard"]
        callbacks = {button["callback_data"] for row in rows for button in row}
        self.assertEqual(callbacks, {
            "platform_youtube", "platform_instagram", "platform_facebook",
            "platform_snapchat", "comments", "settings", "my_accounts",
        })

    def test_my_accounts_formats_connected_channels_for_telegram(self):
        message = format_youtube_accounts([{"account_name": "My Channel", "channel_id": "UC123"}])
        self.assertIn("My Channel", message)
        self.assertIn("UC123", message)

    def test_my_accounts_shows_clear_empty_state(self):
        self.assertIn("Abhi connected nahi", format_youtube_accounts([]))

    def test_menu_greeting_uses_first_name(self):
        self.assertIn("Welcome Yogesh", main_menu_text("Yogesh"))

    def test_unavailable_platform_is_not_claimed_connected(self):
        message = callback_response("platform_instagram")
        self.assertIn("abhi available nahi hai", message)

    def test_unknown_callback_gets_safe_fallback(self):
        self.assertIn("/menu", callback_response("unknown"))



class YouTubeOAuthSecurityTests(unittest.TestCase):
    def setUp(self):
        OAUTH_STATES.clear()
        self.env = patch.dict(os.environ, {
            "OAUTH_STATE_SECRET": "test-only-state-secret",
            "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        OAUTH_STATES.clear()

    def test_oauth_state_is_signed_and_bound_to_telegram_user(self):
        state = make_youtube_oauth_state(123456)
        result = read_youtube_oauth_state(state)
        self.assertEqual(result["user_id"], 123456)
        self.assertEqual(read_youtube_oauth_state(state)["user_id"], 123456)
        self.assertIsNone(read_youtube_oauth_state(state + "tampered"))

    def test_refresh_token_is_encrypted_and_recoverable(self):
        key = os.environ["TOKEN_ENCRYPTION_KEY"]
        encrypted = encrypt_refresh_token("refresh-token-test")
        self.assertNotEqual(encrypted, "refresh-token-test")
        from cryptography.fernet import Fernet
        self.assertEqual(Fernet(key.encode()).decrypt(encrypted.encode()).decode(),
                         "refresh-token-test")


if __name__ == "__main__":
    unittest.main()
