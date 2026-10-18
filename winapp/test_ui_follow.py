"""P 阶段：原始帧控制台「暂停/恢复自动滚动」的纯状态函数。"""
import unittest

import atk_hp20 as app_mod


class TestShouldFollow(unittest.TestCase):
    def test_at_bottom_follows(self):
        self.assertTrue(app_mod.should_follow(True, 1.0))
        self.assertTrue(app_mod.should_follow(True, 0.9995))

    def test_scrolled_up_pauses(self):
        self.assertFalse(app_mod.should_follow(True, 0.4))

    def test_manual_pause_wins(self):
        self.assertFalse(app_mod.should_follow(False, 1.0))

    def test_manual_resume_at_bottom_follows(self):
        """点「恢复」后（会 see("end")）应重新跟随。"""
        self.assertFalse(app_mod.should_follow(False, 1.0))
        self.assertTrue(app_mod.should_follow(True, 1.0))

    def test_scroll_at_bottom_threshold(self):
        self.assertTrue(app_mod.scroll_at_bottom(1.0))
        self.assertFalse(app_mod.scroll_at_bottom(0.98))
        self.assertTrue(app_mod.scroll_at_bottom(0.98, threshold=0.9))


if __name__ == "__main__":
    unittest.main()
