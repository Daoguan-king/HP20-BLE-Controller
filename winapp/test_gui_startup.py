"""GUI 启动排程决策（BUG：只给了 --address 却不自动连接）。"""
import unittest

import atk_hp20 as app_mod


def opts(**kw):
    return app_mod.build_parser().parse_args([]).__class__(**{
        **vars(app_mod.build_parser().parse_args([])), **kw})


class TestShouldAutoConnect(unittest.TestCase):
    def base(self, **kw):
        args = app_mod.build_parser().parse_args([])
        for key, value in kw.items():
            setattr(args, key, value)
        return args

    def test_sim_auto_connects(self):
        self.assertTrue(app_mod.should_auto_connect(self.base(sim=True)))

    def test_address_auto_connects(self):
        """真机验收 BUG：带 --address 启动必须自动连接。"""
        self.assertTrue(app_mod.should_auto_connect(
            self.base(address="AA:BB:CC:DD:EE:FF")))

    def test_name_auto_connects(self):
        self.assertTrue(app_mod.should_auto_connect(self.base(name="HP20")))

    def test_plain_start_waits_for_user(self):
        self.assertFalse(app_mod.should_auto_connect(self.base()))

    def test_address_plus_smoke_auto_connects(self):
        """真机复现的正是这一条：--address + --smoke 40 时必须发起连接。"""
        self.assertTrue(app_mod.should_auto_connect(
            self.base(address="AA:BB:CC:DD:EE:FF", smoke=40.0)))

    def test_smoke_heat_only_for_sim(self):
        self.assertTrue(app_mod.should_smoke_heat(self.base(sim=True, smoke=6.0)))
        self.assertFalse(app_mod.should_smoke_heat(self.base(smoke=6.0)))
        self.assertFalse(app_mod.should_smoke_heat(self.base(sim=True, smoke=1.0)))


class TestScheduleStartup(unittest.TestCase):
    """用可注入的 schedule 替身断言「排了哪些启动任务」。"""

    def run_schedule(self, args):
        calls = []
        app_mod.schedule_startup(
            args, lambda delay, fn: calls.append((delay, fn)),
            auto_connect="auto", smoke_heat="heat", smoke_done="done")
        return calls

    def base(self, **kw):
        args = app_mod.build_parser().parse_args([])
        for key, value in kw.items():
            setattr(args, key, value)
        return args

    def test_address_schedules_auto_connect(self):
        calls = self.run_schedule(self.base(address="AA:BB:CC:DD:EE:FF"))
        self.assertEqual(calls, [(150, "auto")])

    def test_sim_schedules_auto_connect(self):
        self.assertEqual(self.run_schedule(self.base(sim=True)), [(150, "auto")])

    def test_plain_schedules_nothing(self):
        self.assertEqual(self.run_schedule(self.base()), [])

    def test_sim_smoke_schedules_heat_and_done(self):
        calls = self.run_schedule(self.base(sim=True, smoke=6.0))
        self.assertEqual([c[1] for c in calls], ["auto", "heat", "done"])
        self.assertEqual(calls[-1][0], 6000)

    def test_real_smoke_schedules_connect_and_done_only(self):
        calls = self.run_schedule(self.base(address="AA:BB:CC:DD:EE:FF", smoke=40.0))
        self.assertEqual([c[1] for c in calls], ["auto", "done"])
        self.assertEqual(calls[-1][0], 40000)


class TestLooksLikeAddress(unittest.TestCase):
    def test_true(self):
        self.assertTrue(app_mod.looks_like_address("AA:BB:CC:DD:EE:FF"))
        self.assertTrue(app_mod.looks_like_address("aa:bb:cc:dd:ee:ff"))

    def test_false(self):
        self.assertFalse(app_mod.looks_like_address("HP20"))
        self.assertFalse(app_mod.looks_like_address("11:22:33:44:55"))
        self.assertFalse(app_mod.looks_like_address(""))


if __name__ == "__main__":
    unittest.main()
