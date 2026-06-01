from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram.constants import ParseMode
from telegram.error import BadRequest

from src.telegram_bot import _markdown_link, _safe_send, _safe_send_photo, send_profile_change


class TelegramBotFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_send_falls_back_to_plain_text_on_parse_error(self) -> None:
        bot = AsyncMock()
        bot.send_message = AsyncMock(
            side_effect=[
                BadRequest("Can't parse entities: character '(' is reserved and must be escaped with the preceding '\\'"),
                object(),
            ]
        )

        delivered = await _safe_send(bot, "-1001", r"Before: _\(empty\)_")

        self.assertTrue(delivered)
        self.assertEqual(bot.send_message.await_count, 2)
        first_kwargs = bot.send_message.await_args_list[0].kwargs
        second_kwargs = bot.send_message.await_args_list[1].kwargs
        self.assertEqual(first_kwargs["parse_mode"], ParseMode.MARKDOWN_V2)
        self.assertEqual(second_kwargs["text"], "Before: (empty)")
        self.assertNotIn("parse_mode", second_kwargs)

    async def test_safe_send_photo_falls_back_to_plain_caption_on_parse_error(self) -> None:
        bot = AsyncMock()
        bot.send_photo = AsyncMock(
            side_effect=[
                BadRequest("Can't parse entities: character '(' is reserved and must be escaped with the preceding '\\'"),
                object(),
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            photo_path = Path(temp_dir) / "photo.jpg"
            photo_path.write_bytes(b"not-a-real-jpeg")

            delivered = await _safe_send_photo(bot, "-1001", photo_path, caption=r"Target: [@alice](https://example.com/test\))")

        self.assertTrue(delivered)
        self.assertEqual(bot.send_photo.await_count, 2)
        first_kwargs = bot.send_photo.await_args_list[0].kwargs
        second_kwargs = bot.send_photo.await_args_list[1].kwargs
        self.assertEqual(first_kwargs["parse_mode"], ParseMode.MARKDOWN_V2)
        self.assertEqual(second_kwargs["caption"], "Target: @alice: https://example.com/test)")
        self.assertNotIn("parse_mode", second_kwargs)

    async def test_send_profile_change_escapes_empty_placeholder(self) -> None:
        with patch("src.telegram_bot._safe_send", new=AsyncMock(return_value=True)) as safe_send:
            delivered = await send_profile_change(
                bot=AsyncMock(),
                chat_id="-1001",
                target_username="alice",
                field="bio",
                old_value=None,
                new_value="Updated bio",
                platform="instagram",
            )

        self.assertTrue(delivered)
        sent_text = safe_send.await_args.args[2]
        self.assertIn(r"Before: _\(empty\)_", sent_text)


class TelegramBotLinkTests(unittest.TestCase):
    def test_markdown_link_escapes_url_closing_parenthesis(self) -> None:
        link = _markdown_link("View post", "https://example.com/post/test)")

        self.assertEqual(link, r"[View post](https://example.com/post/test\))")


if __name__ == "__main__":
    unittest.main()
