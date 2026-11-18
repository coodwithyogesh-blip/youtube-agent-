import unittest

from app import callback_response, main_menu_keyboard, main_menu_text


class TelegramMenuTests(unittest.TestCase):
    def test_main_menu_has_all_requested_sections(self):
        rows = main_menu_keyboard()["inline_keyboard"]
        callbacks = {button["callback_data"] for row in rows for button in row}
        self.assertEqual(callbacks, {
            "platform_youtube", "platform_instagram", "platform_facebook",
            "platform_snapchat", "comments", "settings",
        })

    def test_menu_greeting_uses_first_name(self):
        self.assertIn("Welcome Yogesh", main_menu_text("Yogesh"))

    def test_unavailable_platform_is_not_claimed_connected(self):
        message = callback_response("platform_instagram")
        self.assertIn("abhi available nahi hai", message)

    def test_unknown_callback_gets_safe_fallback(self):
        self.assertIn("/menu", callback_response("unknown"))


if __name__ == "__main__":
    unittest.main()
