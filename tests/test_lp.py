"""运输 LP 求解器的正确性测试（含手工核算值）。"""
import unittest

from settle.lp import TransportationLP, Infeasible


def two_box():
    return [
        {"box": "b1", "low": 9, "high": 11,
         "cells": [(0, 0)], "prices": {(0, 0): 3.0}},
        {"box": "b2", "low": 9, "high": 11,
         "cells": [(0, 0)], "prices": {(0, 0): 4.0}},
    ]


class LPTest(unittest.TestCase):
    def test_min_max_endpoints_have_single_allocation(self):
        p = TransportationLP(two_box(), {(0, 0): 2}, (18, 22),
                             (0, 1e9), False)
        lo, lo_w, _ = p.solve("min")
        hi, hi_w, _ = p.solve("max")
        # min：总量压到段下限 18，各保下界 9
        self.assertAlmostEqual(lo, 63.0)
        self.assertEqual([round(w) for w in lo_w], [9, 9])
        # max：总量推到 22，两箱都触上界 11（每箱上界 11 的硬约束）
        self.assertAlmostEqual(hi, 77.0)
        self.assertEqual([round(w) for w in hi_w], [11, 11])

    def test_order_bound_changes_feasible_total(self):
        p = TransportationLP(two_box(), {(0, 0): 2}, (18, 22),
                             (20, 21), True)
        lo, lo_w, _ = p.solve("min")
        hi, hi_w, _ = p.solve("max")
        # 总量下限 20：增量 2 给低价箱 b1 => b1=11,b2=9
        self.assertAlmostEqual(lo, 69.0)
        # 总量上限 21：增量给高价箱 b2 => b1=10,b2=11
        self.assertAlmostEqual(hi, 74.0)

    def test_high_price_box_gets_increment_first(self):
        # 三箱同格不同价，max 增量应优先高价箱
        boxes = two_box() + [
            {"box": "b3", "low": 9, "high": 11,
             "cells": [(0, 0)], "prices": {(0, 0): 5.0}}]
        p = TransportationLP(boxes, {(0, 0): 3}, (27, 33),
                             (0, 1e9), False)
        _, weights, _ = p.solve("max")
        # 高价箱 b3 先灌满上界
        self.assertAlmostEqual(weights[2], 11.0)

    def test_infeasible_when_lower_exceeds_segment_high(self):
        boxes = two_box()
        p = TransportationLP(boxes, {(0, 0): 2}, (10, 15),
                             (0, 1e9), False)
        with self.assertRaises(Infeasible):
            p.solve("min")

    def test_cell_capacity_blocks_overflow(self):
        # 两箱只能进同一格，但格容量按 1 箱折算，应不可行
        boxes = two_box()
        p = TransportationLP(boxes, {(0, 0): 1}, (18, 22),
                             (0, 1e9), False)
        with self.assertRaises(Infeasible):
            p.solve("min")

    def test_multi_cell_routing_uses_cheapest_for_min(self):
        boxes = [
            {"box": "b1", "low": 9, "high": 11,
             "cells": [(0, 0), (0, 1)],
             "prices": {(0, 0): 2.0, (0, 1): 7.0}},
            {"box": "b2", "low": 9, "high": 11,
             "cells": [(0, 0), (0, 1)],
             "prices": {(0, 0): 2.0, (0, 1): 7.0}},
        ]
        p = TransportationLP(boxes, {(0, 0): 2, (0, 1): 2},
                             (18, 22), (0, 1e9), False)
        lo, _, alloc = p.solve("min")
        # min 全部走低价格
        self.assertAlmostEqual(lo, 36.0)
        self.assertTrue(all(row[1] == 0 for row in alloc))


if __name__ == "__main__":
    unittest.main()
