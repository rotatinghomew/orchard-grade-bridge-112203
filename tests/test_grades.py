"""分档几何与跨档复核的单元测试。"""
import unittest

from settle.grades import GradeScheme, regions_intersect


POLICY = {"min_box_kg": 9, "max_box_kg": 11,
         "diameter_domain": [55, 100], "color_domain": [0, 1]}
OLD = {"id": "old", "effective": "2026-01-01",
       "diameter_edges": [65, 75], "color_edges": [0.45, 0.65],
       "price": [[2, 2, 2], [2, 3, 4], [2, 4, 5]]}
NEW = {"id": "new", "effective": "2026-10-01",
       "diameter_edges": [67, 78], "color_edges": [0.5, 0.7],
       "price": [[2, 2, 2], [2, 4, 6], [3, 5, 7]]}


class GradeTest(unittest.TestCase):
    def setUp(self):
        self.old = GradeScheme.from_contract(OLD, POLICY)
        self.new = GradeScheme.from_contract(NEW, POLICY)

    def test_left_closed_right_open(self):
        # 边界点归入右侧高档
        self.assertEqual(self.old.band_index(65, "d"), 1)
        self.assertEqual(self.old.band_index(75, "d"), 2)
        self.assertEqual(self.old.band_index(64.9, "d"), 0)
        self.assertEqual(self.old.band_index(0.45, "c"), 1)

    def test_interval_crossing_boundary_keeps_both_bands(self):
        # 复核区间跨过 75 边界，不能假设居中，必须保留两侧档
        bands = self.old.bands_overlapped(74, 78, "d")
        self.assertEqual(bands, (1, 2))
        # 完全落在单档内
        self.assertEqual(self.old.bands_overlapped(70, 72, "d"), (1,))

    def test_joint_cells_overlapped(self):
        cells = self.old.cells_overlapped(74, 78, 0.6, 0.7)
        self.assertIn((1, 1), cells)
        self.assertIn((2, 1), cells)
        self.assertIn((1, 2), cells)
        self.assertIn((2, 2), cells)

    def test_region_intersection_prevents_whole_cell_move(self):
        # 旧格 (2,2) 与新格 (0,0) 在物理平面无交集，禁止整格搬运
        self.assertFalse(regions_intersect(
            self.old, (2, 2), self.new, (0, 0)))
        # 旧高档果径区与新高档果径区有交集
        self.assertTrue(regions_intersect(
            self.old, (2, 1), self.new, (2, 1)))


if __name__ == "__main__":
    unittest.main()
