"""BLE 特征选择：真机 GATT（fff0/fff1）与 APK 字符串（ff01/ff02/ffff）不同，
因此这里锁死"按属性挑"的行为，防止以后又退回按 UUID 写死。
"""
import unittest

from transport import BleTransport, TransportError


class FakeChar:
    def __init__(self, uuid: str, handle: int, properties):
        self.uuid = uuid
        self.handle = handle
        self.properties = list(properties)


class FakeServices:
    def __init__(self, chars):
        self.characteristics = {c.handle: c for c in chars}


class FakeClient:
    def __init__(self, chars):
        self.services = FakeServices(chars)


REAL_HP20 = [
    FakeChar("0000fff1-0000-1000-8000-00805f9b34fb", 11,
             ["notify", "write-without-response"]),
    FakeChar("0000fff2-0000-1000-8000-00805f9b34fb", 12, ["notify", "read"]),
    FakeChar("0000fff3-0000-1000-8000-00805f9b34fb", 13,
             ["write-without-response"]),
]


def resolve(chars):
    transport = BleTransport()
    transport._client = FakeClient(chars)
    transport._resolve_chars()
    return transport


class TestCharacteristicDiscovery(unittest.TestCase):
    def test_real_device_single_char_used_for_both_directions(self):
        transport = resolve(REAL_HP20)
        self.assertIs(transport._notify_char, REAL_HP20[0])
        self.assertIs(transport._write_char, REAL_HP20[0])
        self.assertFalse(transport.write_with_response)

    def test_separate_notify_and_write_chars(self):
        chars = [
            FakeChar("0000ff02-0000-1000-8000-00805f9b34fb", 5, ["notify"]),
            FakeChar("0000ffff-0000-1000-8000-00805f9b34fb", 6,
                     ["write-without-response"]),
        ]
        transport = resolve(chars)
        self.assertEqual(transport._notify_char.uuid[:8], "0000ff02")
        self.assertEqual(transport._write_char.uuid[:8], "0000ffff")

    def test_write_with_chars_only(self):
        chars = [FakeChar("0000aaa1-0000-1000-8000-00805f9b34fb", 7, ["notify", "write"])]
        transport = resolve(chars)
        self.assertIs(transport._notify_char, chars[0])
        self.assertIs(transport._write_char, chars[0])

    def test_prefers_write_without_response(self):
        plain = FakeChar("0000aaaa-0000-1000-8000-00805f9b34fb", 1, ["write"])
        fast = FakeChar("0000bbbb-0000-1000-8000-00805f9b34fb", 2,
                        ["write-without-response"])
        transport = resolve([plain, fast, FakeChar("0000cccc-0000-1000-8000-00805f9b34fb",
                                                   3, ["notify"])])
        self.assertIs(transport._write_char, fast)

    def test_indicate_counts_as_notify(self):
        chars = [FakeChar("0000dddd-0000-1000-8000-00805f9b34fb", 1, ["indicate"]),
                 FakeChar("0000eeee-0000-1000-8000-00805f9b34fb", 2, ["write"])]
        self.assertIs(resolve(chars)._notify_char, chars[0])

    def test_explicit_uuid_override_wins(self):
        transport = BleTransport(notify_uuid=REAL_HP20[1].uuid,
                                write_uuid=REAL_HP20[2].uuid)
        transport._client = FakeClient(REAL_HP20)
        transport._resolve_chars()
        self.assertIs(transport._notify_char, REAL_HP20[1])
        self.assertIs(transport._write_char, REAL_HP20[2])

    def test_missing_notify_lists_full_table(self):
        chars = [FakeChar("0000ffff-0000-1000-8000-00805f9b34fb", 9, ["write"])]
        transport = BleTransport()
        transport._client = FakeClient(chars)
        with self.assertRaises(TransportError) as ctx:
            transport._resolve_chars()
        message = str(ctx.exception)
        self.assertIn("0000ffff", message)
        self.assertIn("handle=9", message)

    def test_missing_write_lists_full_table(self):
        chars = [FakeChar("0000ff02-0000-1000-8000-00805f9b34fb", 4, ["notify"])]
        transport = BleTransport()
        transport._client = FakeClient(chars)
        with self.assertRaises(TransportError) as ctx:
            transport._resolve_chars()
        self.assertIn("0000ff02", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
