"""动态场景：补交逐箱证据后，确定款与共用额度重新分配。"""
import copy
import unittest
from pathlib import Path

from settle.ingest import load_packet
from settle.feasible import assess_order
from settle.pools import Ledger, plan_settlement

PACKET = Path(__file__).resolve().parent.parent / "资料" / "演练.json"


class ReEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.base = load_packet(PACKET)

    def _plans(self, result):
        assessments = {oid: assess_order(result, oid)
                       for oid in result.orders}
        ledger = Ledger.from_packet(result.packet)
        return plan_settlement(result, assessments, ledger)

    def test_supplement_box_evidence_grows_certain_income(self):
        before = assess_order(self.base, "D2").certain_amount
        # 把旧格 (1,2) 的一个匿名箱细化为逐箱读数（不增加箱数）：
        # d70 在新合同档1、c0.8 在档2 => 新格 (1,2) 价 6
        packet2 = copy.deepcopy(self.base.packet)
        packet2["boxes"].append({
            "shipment": "D2", "segment": "A", "box": "D2-AX9",
            "diameter": 70, "color": 0.8, "kg": 10.5, "loaded": "2026-10-07",
        })
        result2 = load_packet(_write(packet2))
        after = assess_order(result2, "D2").certain_amount
        self.assertAlmostEqual(after - before, 6 * 10.5)

    def test_new_certain_claim_consumes_shared_pool_then_starves_later(self):
        # 构造一个额度紧张的池，验证补交后新增确定款占用准备额、
        # 排队靠后的订单拿不到重复额度
        packet = copy.deepcopy(self.base.packet)
        # 把池 A 准备额压到很小，确定款额度也收紧
        for pool in packet["pools"]:
            if pool["pool"] == "A":
                pool["advance_ceiling"] = 400   # 已占 360，只剩 40
                pool["hold_ceiling"] = 20
        result = load_packet(_write(packet))
        plans = self._plans(result)
        pool_a = plans["A"]
        # 剩余确定款额度只有 40，新增签发不得超过它
        total_new = sum(o.new_signable for o in pool_a.orders)
        self.assertLessEqual(total_new, 40.0 + 1e-9)
        # 准备额 20 全给优先序第一的 D1
        self.assertAlmostEqual(pool_a.orders[0].hold_granted, 20.0)
        self.assertEqual(pool_a.orders[1].hold_granted, 0.0)
        # 没有重复占额：已用 + 新增不超过天花板
        self.assertLessEqual(
            pool_a.advance_used_before + total_new,
            pool_a.advance_ceiling + 1e-9)

    def test_blocked_order_does_not_use_estimated_value_for_advance(self):
        # D3 整单不可结，其确定款签发必须为 0，不能拿估值占额
        plans = self._plans(self.base)
        d3 = next(o for o in plans["B"].orders if o.order_id == "D3")
        self.assertEqual(d3.new_signable, 0.0)
        self.assertEqual(d3.certain, 0.0)
        # 但池 B 额度仍保留给未来可结批次
        self.assertEqual(plans["B"].advance_remaining, 300.0)


def _write(packet):
    import json
    import tempfile
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(packet, handle, ensure_ascii=False)
    handle.close()
    return handle.name


if __name__ == "__main__":
    unittest.main()
