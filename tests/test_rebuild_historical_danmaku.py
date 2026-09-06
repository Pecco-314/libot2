from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import msgpack

from scripts import rebuild_historical_danmaku as rebuild


class _Response:
    def __init__(self, *, content: bytes = b"", payload: Any = None) -> None:
        self.content = content
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Response:
        del headers
        self.calls.append((url, params))
        return self.responses.pop(0)


class DanmakusEmojiResolutionTest(unittest.TestCase):
    def test_legacy_header_maps_resource_to_rendered_name(self) -> None:
        resource = "/bfs/garb/shark.png"
        payload = {
            "data": {
                "frame": {
                    "roomEmojis": [
                        {
                            "name": "upower_[炸了吧  甲鱼班班_开心]",
                            "resource": resource,
                        }
                    ]
                }
            }
        }
        self.assertEqual(
            rebuild._legacy_emoji_names(payload, {resource}),
            {resource: "[upower_[炸了吧  甲鱼班班_开心]]"},
        )

    def test_full_fetch_resolves_unknown_emoji_from_json_header(self) -> None:
        resource = "/bfs/garb/new-shark.png"
        packed = msgpack.packb(
            [
                [rebuild.CHANNEL_UID, "三理Mit3uri", rebuild.ROOM_ID],
                ["live-id", "title", "", 1_000, 0, False, False, False, 1],
                [
                    1_000,
                    [[123, "viewer"]],
                    [[resource]],
                    [[0, 0, 1, 0, 2, [1]]],
                ],
            ],
            use_bin_type=True,
        )
        legacy = {
            "data": {
                "hasMore": False,
                "frame": {
                    "records": [{}],
                    "roomEmojis": [
                        {
                            "name": "upower_[test_开心]",
                            "resource": resource,
                        }
                    ],
                },
            }
        }
        client = _Client(
            [_Response(content=packed), _Response(payload=legacy)]
        )
        emoji_map: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as directory:
            result = rebuild._fetch_danmakus_live(
                client,  # type: ignore[arg-type]
                {"liveId": "live-id", "danmakusCount": 1},
                Path(directory),
                cutoff_ms=2_000,
                emoji_map=emoji_map,
            )
            row = json.loads((Path(directory) / "live-id.jsonl").read_text())

        self.assertEqual(row["content"], "[upower_[test_开心]]")
        self.assertEqual(emoji_map[resource], "[upower_[test_开心]]")
        self.assertEqual(result["resolved_emojis"], 1)
        self.assertEqual(result["unknown_emojis"], [])
        self.assertEqual(result["emoji_metadata_pages"], 1)
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
