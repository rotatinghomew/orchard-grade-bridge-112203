"""分档几何：边界左闭右开，最高档包含有效域上限。

读数只有一个值时按边界直接落档；计量复核给出的是闭区间 [low, high]，
区间可能跨过边界，因此可行档是 *区间* 与各档定义域有真实交集的那些档，
绝不把跨档误差悄悄取成区间中点。
"""
from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-9


@dataclass(frozen=True)
class GradeScheme:
    """一套果径/色面联合分档及其价格矩阵（元/千克）。"""

    id: str
    effective: str
    diameter_edges: tuple[float, ...]
    color_edges: tuple[float, ...]
    price: tuple[tuple[float, ...], ...]
    diameter_domain: tuple[float, float]
    color_domain: tuple[float, float]

    @classmethod
    def from_contract(cls, contract: dict, policy: dict) -> "GradeScheme":
        dlo, dhi = policy["diameter_domain"]
        clo, chi = policy["color_domain"]
        return cls(
            id=contract["id"],
            effective=contract["effective"],
            diameter_edges=tuple(contract["diameter_edges"]),
            color_edges=tuple(contract["color_edges"]),
            price=tuple(tuple(row) for row in contract["price"]),
            diameter_domain=(dlo, dhi),
            color_domain=(clo, chi),
        )

    # ---- 单维度分档 ----
    def _bands(self, edges: tuple[float, ...], domain: tuple[float, float]):
        lo, hi = domain
        bounds = [lo, *edges, hi]
        return tuple((bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1))

    @property
    def diameter_bands(self):
        return self._bands(self.diameter_edges, self.diameter_domain)

    @property
    def color_bands(self):
        return self._bands(self.color_edges, self.color_domain)

    def band_index(self, value: float, kind: str) -> int:
        """单点读数落档，边界点归入右侧（高档）一档，即左闭右开。"""
        edges = self.diameter_edges if kind == "d" else self.color_edges
        domain = self.diameter_domain if kind == "d" else self.color_domain
        lo, hi = domain
        if not (lo - EPS <= value <= hi + EPS):
            raise ValueError(f"{kind} 读数 {value} 超出有效域 {domain}")
        index = 0
        for edge in edges:
            if value + EPS >= edge:
                index += 1
        return index

    def bands_overlapped(self, low: float, high: float, kind: str) -> tuple[int, ...]:
        """闭区间 [low, high] 能落入哪些档（与档定义域有交集即可）。"""
        if low > high + EPS:
            raise ValueError(f"复核区间反向：[{low}, {high}]")
        bands = self.diameter_bands if kind == "d" else self.color_bands
        hit = []
        for idx, (blo, bhi) in enumerate(bands):
            # 交集非空：low < bhi 且 high > blo；边界单点相接也算（读数可恰好等于边界）
            if low < bhi - EPS and high > blo + EPS:
                hit.append(idx)
            elif abs(low - bhi) <= EPS or abs(high - blo) <= EPS:
                hit.append(idx)
        return tuple(hit)

    # ---- 联合格子 ----
    @property
    def cells(self):
        return [(i, j) for i in range(len(self.diameter_bands))
                for j in range(len(self.color_bands))]

    def cell_of_reading(self, diameter: float, color: float) -> tuple[int, int]:
        return self.band_index(diameter, "d"), self.band_index(color, "c")

    def cells_overlapped(
        self,
        d_low: float,
        d_high: float,
        c_low: float,
        c_high: float,
    ) -> tuple[tuple[int, int], ...]:
        """一个读数矩形（区间复核后）与哪些联合格子相交。"""
        ds = set(self.bands_overlapped(d_low, d_high, "d"))
        cs = set(self.bands_overlapped(c_low, c_high, "c"))
        return tuple((i, j) for (i, j) in self.cells if i in ds and j in cs)

    def price_at(self, cell: tuple[int, int]) -> float:
        i, j = cell
        return self.price[i][j]


def regions_intersect(
    scheme_a: GradeScheme,
    cell_a: tuple[int, int],
    scheme_b: GradeScheme,
    cell_b: tuple[int, int],
) -> bool:
    """两套分档下的两个联合格子在物理读数平面上是否有真实交集。"""
    ad = scheme_a.diameter_bands[cell_a[0]]
    ac = scheme_a.color_bands[cell_a[1]]
    bd = scheme_b.diameter_bands[cell_b[0]]
    bc = scheme_b.color_bands[cell_b[1]]
    d_overlap = min(ad[1], bd[1]) - max(ad[0], bd[0]) >= -EPS
    c_overlap = min(ac[1], bc[1]) - max(ac[0], bc[0]) >= -EPS
    return d_overlap and c_overlap
