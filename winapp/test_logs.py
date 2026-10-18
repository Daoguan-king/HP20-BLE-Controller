"""CSV 编码 / 列头 / 简洁格式（含 Excel 乱码回归）。"""
import csv
import os
import tempfile
import time
import unittest

import hp20_commands as hc
import logs


def status(temp=50, power=44, supply=45, w2=1229, mode=2, target=60):
    return hc.parse_realtime(hc.build_realtime_payload(
        current_temp=temp, supply_power_w=supply, output_power_w=power,
        target_temp=target, channel_or_stage=0, mode=mode, w2=w2,
        input_voltage_raw=271, output_state_raw=0x02))


class TestEncoding(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_full_csv_has_utf8_bom(self):
        """Excel 按 GBK 解释无 BOM 的 UTF-8 会乱码 -> 必须写 utf-8-sig。"""
        path = os.path.join(self.dir, "full.csv")
        rec = logs.CsvRecorder(path)
        rec.record(status())
        rec.close()
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(3), b"\xef\xbb\xbf")

    def test_compact_csv_has_utf8_bom(self):
        path = os.path.join(self.dir, "compact.csv")
        rec = logs.CompactCsvRecorder(path)
        rec.record(status())
        rec.close()
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(3), b"\xef\xbb\xbf")

    def test_chinese_decodes_after_stripping_bom(self):
        path = os.path.join(self.dir, "full.csv")
        rec = logs.CsvRecorder(path)
        rec.record(status(mode=4))
        rec.close()
        text = open(path, encoding="utf-8-sig").read()
        self.assertIn("维修", text)


class TestFullCsv(unittest.TestCase):
    def test_header_and_row(self):
        path = os.path.join(tempfile.mkdtemp(), "full.csv")
        rec = logs.CsvRecorder(path)
        rec.record(status())
        self.assertEqual(rec.close(), 1)
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(tuple(rows[0]), logs.CSV_HEADER)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[1]), len(logs.CSV_HEADER))
        row = dict(zip(rows[0], rows[1]))
        # 表头已中文化（仿手机 APP）；列序与数值不变，只是文字变了
        self.assertEqual(row["当前温度(℃)"], "50")
        self.assertEqual(row["输出功率(W)"], "44")
        self.assertEqual(row["供电功率(W)"], "45")
        self.assertEqual(row["功率占比(%)"], "97.8")
        self.assertEqual(row["计时"], "19:13")     # 1229 -> 64 进制 19:13
        self.assertEqual(row["输出状态"], "加热模式运行中")

    def test_no_fan_column(self):
        self.assertNotIn("fan_on", logs.CSV_HEADER)
        self.assertFalse(any("fan" in c for c in logs.CSV_HEADER))


class TestCompactCsv(unittest.TestCase):
    def test_header_matches_phone_app(self):
        self.assertEqual(logs.COMPACT_HEADER, ("时间", "V(℃)", "A(W)"))

    def test_row_format(self):
        path = os.path.join(tempfile.mkdtemp(), "compact.csv")
        rec = logs.CompactCsvRecorder(path)
        rec.record(status(temp=50, power=0))
        rec.close()
        text = open(path, encoding="utf-8-sig").read().splitlines()
        self.assertEqual(text[0], "时间,V(℃),A(W)")
        stamp, temp, power = text[1].split(",")
        self.assertRegex(stamp, r"^\d{2}:\d{2}:\d{2}:\d{3}$")
        self.assertEqual(temp, "50.0")
        self.assertEqual(power, "0")

    def test_elapsed_time_advances_from_open(self):
        path = os.path.join(tempfile.mkdtemp(), "compact.csv")
        rec = logs.CompactCsvRecorder(path)
        rec.record(status())
        time.sleep(1.1)
        rec.record(status())
        rec.close()
        lines = open(path, encoding="utf-8-sig").read().splitlines()[1:]
        first = int(lines[0].split(",")[0].split(":")[2])
        second = int(lines[1].split(",")[0].split(":")[2])
        self.assertGreaterEqual(second - first, 1)


class TestMakeRecorder(unittest.TestCase):
    def test_kind_selection(self):
        self.assertIsInstance(logs.make_recorder("compact", os.path.join(
            tempfile.mkdtemp(), "a.csv")), logs.CompactCsvRecorder)
        self.assertIsInstance(logs.make_recorder("full", os.path.join(
            tempfile.mkdtemp(), "b.csv")), logs.CsvRecorder)


class TestElapsedText(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(logs._elapsed_text(0), "00:00:00:000")
        self.assertEqual(logs._elapsed_text(1.021), "00:00:01:021")
        self.assertEqual(logs._elapsed_text(3661.5), "01:01:01:500")


if __name__ == "__main__":
    unittest.main()
