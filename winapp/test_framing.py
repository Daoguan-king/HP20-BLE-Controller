"""组帧/LRC/长度驱动解析 —— 用协议文档里的真实帧做硬断言。"""
import unittest

import framing
import hp20_commands as hc

HP = dict(dev_id=hc.DEV_ID, family=hc.FAMILY)          # HP20 = 0x0209
EL = dict(dev_id=0x07, family=0x03)                    # EL15 = 0x0307


class TestBuildAndLrc(unittest.TestCase):
    def test_hp20_poll_realtime(self):
        self.assertEqual(framing.to_hex(hc.frame_realtime()), "AF 09 02 0A 00 3C")

    def test_hp20_identify(self):
        self.assertEqual(framing.to_hex(hc.frame_identify()), "AF 09 02 00 00 46")

    def test_hp20_set_t5_180(self):
        self.assertEqual(framing.to_hex(hc.frame_set_thermostat(4, 180)),
                         "AF 09 02 03 03 04 B4 00 88")

    def test_hp20_set_reflow_16_bytes(self):
        frame = hc.frame_set_reflow([150, 60, 180, 90, 235, 45, 100, 30])
        self.assertEqual(framing.to_hex(frame),
                         "AF 09 02 06 10 96 00 3C 00 B4 00 5A 00 EB 00 2D 00 64 00 1E 00 B6")
        # 长度字段 = 16 → 总长 22
        self.assertEqual(frame[4], 16)
        self.assertEqual(len(frame), 6 + 16)

    def test_el15_baseline_still_valid(self):
        """新签名下 EL15 基准帧逐字节不变（dev 0x0307）。"""
        self.assertEqual(framing.to_hex(framing.build_frame(0x08, b"", **EL)),
                         "AF 07 03 08 00 3F")
        self.assertEqual(framing.to_hex(framing.build_frame(0x03, b"\x01", **EL)),
                         "AF 07 03 03 01 01 42")

    def test_lrc_table(self):
        for payload in (b"", b"\x00", b"\xaf\x09\x02\x0a\x00", bytes(range(20))):
            self.assertEqual((sum(payload) + framing.compute_lrc(payload)) & 0xFF, 0)

    def test_build_rejects_oversize_payload(self):
        with self.assertRaises(ValueError):
            framing.build_frame(0x06, b"\x00" * 256, **HP)

    def test_build_moves_length_with_payload(self):
        for n in (0, 1, 11, 20, 255):
            frame = framing.build_frame(0x0A, b"\x00" * n, **HP)
            self.assertEqual(frame[4], n)
            self.assertEqual(len(frame), 6 + n)


class TestParse(unittest.TestCase):
    def test_roundtrip(self):
        frame = framing.parse_frame(hc.frame_set_thermostat(4, -20))
        self.assertEqual((frame.dev_header, frame.cmd), (0x0209, hc.CMD_SET_THERMOSTAT))
        self.assertEqual(frame.payload[0], 4)
        self.assertEqual(int.from_bytes(frame.payload[1:3], "little", signed=True), -20)
        self.assertFalse(frame.is_response)

    def test_response_header(self):
        raw = hc.response_frame(hc.CMD_IDENTIFY, bytes((0x09, 0x02)))
        self.assertEqual(raw[0], framing.RESP_HEADER)
        frame = framing.parse_frame(raw)
        self.assertTrue(frame.is_response)
        self.assertEqual(frame.dev_header, 0x0209)

    def test_bad_lrc_raises(self):
        raw = bytearray(hc.frame_realtime())
        raw[-1] ^= 0xFF
        with self.assertRaises(ValueError):
            framing.parse_frame(bytes(raw))

    def test_wrong_length_field_raises(self):
        raw = bytearray(hc.frame_realtime())
        raw[4] = 5
        with self.assertRaises(ValueError):
            framing.parse_frame(bytes(raw))

    def test_short_raises(self):
        with self.assertRaises(ValueError):
            framing.parse_frame(b"\xaf\x09\x02")


class TestStreamParser(unittest.TestCase):
    def setUp(self):
        self.payload = hc.build_realtime_payload(
            current_temp=120, supply_power_w=140, output_power_w=17, target_temp=180,
            channel_or_stage=0, mode=2, w2=0x400C)
        self.status = hc.response_frame(hc.CMD_REALTIME, self.payload)

    def test_single_frame(self):
        frames = framing.FrameParser().feed(self.status)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].cmd, hc.CMD_REALTIME)
        self.assertEqual(len(frames[0].payload), 11)

    def test_split_frame_reassembly(self):
        parser = framing.FrameParser()
        self.assertEqual(parser.feed(self.status[:4]), [])
        self.assertEqual(parser.pending, 4)
        frames = parser.feed(self.status[4:])
        self.assertEqual([f.raw for f in frames], [self.status])

    def test_split_byte_by_byte(self):
        parser = framing.FrameParser()
        got = []
        for i in range(len(self.status)):
            got.extend(parser.feed(self.status[i:i + 1]))
        self.assertEqual([f.raw for f in got], [self.status])

    def test_two_frames_one_chunk(self):
        other = hc.response_frame(hc.CMD_GET_THERMOSTAT, bytes(8))
        frames = framing.FrameParser().feed(self.status + other)
        self.assertEqual([f.cmd for f in frames], [hc.CMD_REALTIME, hc.CMD_GET_THERMOSTAT])

    def test_garbage_before_frame_dropped(self):
        parser = framing.FrameParser()
        frames = parser.feed(b"\x00\x11\x22\xaf" + self.status)
        self.assertEqual([f.raw for f in frames], [self.status])
        self.assertGreaterEqual(parser.dropped, 4)

    def test_bad_lrc_dropped_silently_then_recovers(self):
        bad = bytearray(self.status)
        bad[-1] ^= 0xFF
        parser = framing.FrameParser()
        frames = parser.feed(bytes(bad) + self.status)
        self.assertEqual([f.raw for f in frames], [self.status])
        self.assertEqual(parser.bad_lrc, 1)

    def test_corrupt_length_field_resyncs(self):
        """长度字段被写小 -> LRC 必失败 -> 逐字节重同步后仍能拿到下一帧。"""
        bad = bytearray(self.status)
        bad[4] = 0x00
        parser = framing.FrameParser()
        frames = parser.feed(bytes(bad) + self.status)
        self.assertEqual([f.raw for f in frames], [self.status])
        self.assertGreaterEqual(parser.bad_lrc, 1)

    def test_oversize_length_field_waits_instead_of_emitting_garbage(self):
        """长度字段被写大时无从判断，只能等数据（不能瞎产出）。"""
        bad = bytearray(self.status)
        bad[4] = 0xFF
        parser = framing.FrameParser()
        self.assertEqual(parser.feed(bytes(bad) + self.status), [])
        self.assertGreater(parser.pending, 0)

    def test_empty_payload_frame(self):
        ack = hc.response_frame(hc.CMD_SET_THERMOSTAT, b"")
        self.assertEqual(len(ack), 6)
        frames = framing.FrameParser().feed(ack * 3)
        self.assertEqual([f.cmd for f in frames], [hc.CMD_SET_THERMOSTAT] * 3)

    def test_resync_after_truncated_frame(self):
        """半截帧残留 + 后面跟一个完整帧：丢残留，好帧照出。"""
        parser = framing.FrameParser()
        self.assertEqual(parser.feed(self.status[:7]), [])
        frames = parser.feed(self.status)
        self.assertEqual([f.raw for f in frames], [self.status])
        self.assertGreaterEqual(parser.dropped, 7)


if __name__ == "__main__":
    unittest.main()
