from __future__ import annotations

import unittest
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from src.common.meme_image import extract_reply_text


class MemeReplyTest(unittest.TestCase):
    def test_extract_reply_text_ignores_non_text_segments(self) -> None:
        event = SimpleNamespace(
            reply=SimpleNamespace(
                message=Message(
                    [
                        MessageSegment.text("  回复里的内容  "),
                        MessageSegment.image("https://example.com/image.png"),
                    ]
                )
            )
        )
        self.assertEqual(extract_reply_text(event), "回复里的内容")

    def test_extract_reply_text_requires_plain_text(self) -> None:
        event = SimpleNamespace(
            reply=SimpleNamespace(
                message=Message(
                    [MessageSegment.image("https://example.com/image.png")]
                )
            )
        )
        self.assertIsNone(extract_reply_text(event))


if __name__ == "__main__":
    unittest.main()
