"""资料识别、可行域与额度池的业务级测试。"""
import unittest
from pathlib import Path

from settle.ingest import load_packet
from settle.feasible import assess_order, assess_segment
from settle.pools import Ledger, plan_settlement

PACKET = Path(__file__).resolve().parent.parent / "资料" / "演练.json"


class BusinessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = load_packet(PACKET)

    def test_segment_classification(self):
        cls = self.result.classifications
        # D2-A 有两个 revision => duplicate；D1 各段有逐箱 => partial
        self.assertEqual(cls[("D2", "A")], "duplicate")
        self.assertEqual(cls[("D1", "A")], "partial")
        self.assertEqual(cls[("D1", "B")], "partial")
        # D3 无逐箱、单 revision => standalone
        self.assertEqual(cls[("D3", "A")], "standalone")

    def test_revision_replacement_uses_latest_grid(self):
        view = self.result.segments[("D2", "A")]
        # revision2 把 (1,1) 从 4 改为 3、(1,2) 从 1 改为 2
        self.assertEqual(view.grid_cells[1][1], 3)
        self.assertEqual(view.grid_cells[1][2], 2)
        self.assertEqual(view.grid_revision, 2)
        self.assertEqual(len(view.superseded_grids), 1)

    def test_check_interval_replaces_point_but_keeps_other_fields(self):
        box = self.result.segments[("D1", "A")].boxes["D1-A2"]
        # diameter 被区间 [74,78] 替代，color 仍为单点
        self.assertEqual(box.diameter, (74.0, 78.0))
        self.assertEqual(box.color, (0.66, 0.66))
        self.assertEqual(box.kg, 9.9)
        self.assertIn("diameter", box.check_fields)

    def test_certain_income_matches_hand_calc(self):
        # D1: A1 旧档(1,1) 价3×10.2；B1 (1,2) 价4×10；A2 跨档不确定
        a = assess_order(self.result, "D1")
        self.assertAlmostEqual(a.certain_amount, 3 * 10.2 + 4 * 10.0)
        # D2: A1 新档(2,1) 价5×10.1
        b = assess_order(self.result, "D2")
        self.assertAlmostEqual(b.certain_amount, 5 * 10.1)

    def test_endpoints_supported_by_single_allocation(self):
        # 上下限各自来自一次完整 LP 求解，且 low<=high
        for oid in ("D1", "D2"):
            a = assess_order(self.result, oid)
            self.assertLessEqual(a.payable_low, a.payable_high + 1e-9)
            for seg in a.segments:
                if seg.ok:
                    self.assertIsNotNone(seg.witness_low)
                    self.assertIsNotNone(seg.witness_high)

    def test_blocked_segment_d3_is_isolated_but_others_settle(self):
        a = assess_order(self.result, "D3")
        self.assertEqual(len(a.blocked), 1)
        self.assertIn("箱数", a.blocked[0].reasons[0])
        self.assertFalse(a.order_total_verified)
        # 其余订单不受影响
        self.assertTrue(assess_order(self.result, "D1").segments[0].ok)

    def test_alternative_contract_price_diff_range(self):
        a = assess_order(self.result, "D1", ["new"])
        diff = a.alternative["new"]
        self.assertLessEqual(diff["diff_low"], diff["diff_high"] + 1e-9)
        # 新合同价格整体不低于旧合同，价差下界应为非负
        self.assertGreaterEqual(diff["diff_low"], -1e-9)

    def test_pool_protects_existing_and_priority(self):
        assessments = {oid: assess_order(self.result, oid)
                       for oid in self.result.orders}
        ledger = Ledger.from_packet(self.result.packet)
        plans = plan_settlement(self.result, assessments, ledger)
        pool_a = plans["A"]
        # 已有 paid 240 + signed 120 = 360 受保护
        self.assertAlmostEqual(pool_a.advance_used_before, 360.0)
        # D1 日期 09-30 早于 D2 10-02，优先序 D1 在前
        self.assertEqual(pool_a.orders[0].order_id, "D1")
        # hold 额度 310 全给排队在前的 D1，D2 准备额缺口
        self.assertAlmostEqual(pool_a.orders[0].hold_granted, 310.0)
        self.assertGreater(pool_a.orders[1].hold_shortfall, 0)

    def test_prepaid_excess_flagged_not_silently_absorbed(self):
        assessments = {oid: assess_order(self.result, oid)
                       for oid in self.result.orders}
        ledger = Ledger.from_packet(self.result.packet)
        plans = plan_settlement(self.result, assessments, ledger)
        d1 = next(o for o in plans["A"].orders if o.order_id == "D1")
        # 已付 240 超过确定款 70.6
        self.assertAlmostEqual(d1.prepaid_excess, 240 - 70.6, places=2)

    def test_withdraw_only_releases_unsigned(self):
        ledger = Ledger.from_packet(self.result.packet)
        ledger.confirm("auth18")  # signed -> paid
        with self.assertRaises(ValueError):
            ledger.withdraw("auth18")
        # 原 paid 记录金额不变
        self.assertEqual(ledger.paid_total("D2"), 120.0)


if __name__ == "__main__":
    unittest.main()
