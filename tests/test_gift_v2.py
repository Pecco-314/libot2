import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.monitor.monitor import RawEventHandler, _payload_for_log


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


class GiftV2Test(unittest.TestCase):
    def test_ten_draws_enqueue_all_three_results(self):
        root = field(1, 383711170) + field(2, "Pecco丶")
        for name, count in [("棉花糖", 7), ("爱心抱枕", 2), ("电影票", 1)]:
            detail = (field(2, name) + field(3, count) + field(7, count*15000)
                      + field(10, 1789142105))
            root += field(10, detail)
        command = dict(cmd="SEND_GIFT_V2", data=dict(pb=base64.b64encode(root).decode()))
        queue = asyncio.Queue()
        with patch("src.monitor.monitor.logger"):
            RawEventHandler(queue).handle(SimpleNamespace(room_id=1967216004), command)
        rows = [queue.get_nowait() for _ in range(queue.qsize())]
        self.assertEqual([(r[5],r[6],r[7]) for r in rows],
                         [("棉花糖",7,105000),("爱心抱枕",2,30000),("电影票",1,15000)])

    def test_raw_log_does_not_truncate_pb(self):
        import json
        command = dict(cmd="SEND_GIFT_V2", data=dict(pb="a"*20000))
        self.assertEqual(json.loads(_payload_for_log(command)),command)
