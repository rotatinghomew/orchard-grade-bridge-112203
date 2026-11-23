"""运输型线性规划：无下界供需网络 + 势能最小费用流（仅标准库）。

变量 x_{b,c} >= 0：箱 b 投到格 c 的重量。
  l_b <= Σ_c x_{b,c} <= u_b
  Σ_b x_{b,c} <= cap_c * U_c          （格 c 最多 cap_c 箱）
  seg_low <= Σ x <= seg_high，整单范围同理。

网络（无下界，硬下界用超源/超汇供需表达）：
- 源->箱容量 (u_b-l_b)；超源 SS->箱容量 l_b，代表下界重量必须送出；
- 箱->格带 ±单价；格->段带箱数折算容量；段->单->汇带称重范围；
- 汇->超汇 TT 容量为硬下限，验证可行流。
min 直接最小化费用；max 把费用取负后同样最小化。残量网络的负边由
Johnson 势能转为非负，Dijkstra 增广；网络无负费用环，势能一次初始化即可。
"""
from __future__ import annotations

import heapq

EPS = 1e-9


class Infeasible(Exception):
    """约束无法同时成立。"""


class _Edge:
    __slots__ = ("to", "rev", "cap", "cost", "initial_cap")

    def __init__(self, to, rev, cap, cost):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost
        self.initial_cap = cap


class TransportationLP:
    def __init__(self, boxes, cell_capacity, seg_bounds, order_bounds,
                 apply_order_bound):
        self.boxes = boxes
        self.cell_list = sorted(cell_capacity)
        self.cell_capacity = cell_capacity
        self.seg_bounds = seg_bounds
        self.order_bounds = order_bounds
        self.apply_order_bound = apply_order_bound

        self.s = 0
        self.box_base = 1
        self.cell_base = self.box_base + len(boxes)
        self.seg_node = self.cell_base + len(self.cell_list)
        if apply_order_bound:
            self.order_node = self.seg_node + 1
            self.t = self.order_node + 1
        else:
            self.order_node = None
            self.t = self.seg_node + 1
        # 阶段一可行性：超汇 TT 只接收硬下限；阶段二业务流用真实汇 t。
        # 二者共享箱/格/段网络，但出口边独立，互不占用容量。
        self.ss = self.t + 1
        self.tt = self.t + 2
        self.choice_edges = {}

    def _add(self, g, u, v, cap, cost):
        f = _Edge(v, len(g[v]), cap, cost)
        r = _Edge(u, len(g[u]), 0.0, -cost)
        g[u].append(f)
        g[v].append(r)
        return f

    def _build(self, sense):
        g = [[] for _ in range(self.tt + 1)]
        big = sum(b["high"] for b in self.boxes) + 1.0
        for bi, b in enumerate(self.boxes):
            self._add(g, self.s, self.box_base + bi,
                      b["high"] - b["low"], 0.0)
        self.choice_edges = {}
        for bi, b in enumerate(self.boxes):
            for cell in b["cells"]:
                ci = self.cell_list.index(cell)
                cost = -b["prices"][cell] if sense == "max" else b["prices"][cell]
                edge = self._add(g, self.box_base + bi,
                                 self.cell_base + ci, big, cost)
                self.choice_edges[(bi, cell)] = edge
        for ci, cell in enumerate(self.cell_list):
            cap_boxes = self.cell_capacity[cell]
            max_u = max((b["high"] for b in self.boxes if cell in b["cells"]),
                        default=0.0)
            self._add(g, self.cell_base + ci, self.seg_node,
                      cap_boxes * max_u, 0.0)
        seg_lo, seg_hi = self.seg_bounds
        if self.apply_order_bound:
            o_lo, o_hi = self.order_bounds
            self._add(g, self.seg_node, self.order_node, seg_hi, 0.0)
            self._add(g, self.order_node, self.t, o_hi, 0.0)
            sink_floor = o_lo
        else:
            self._add(g, self.seg_node, self.t, seg_hi, 0.0)
            sink_floor = seg_lo
        for bi, b in enumerate(self.boxes):
            self._add(g, self.ss, self.box_base + bi, b["low"], 0.0)
        # t->TT 给完整容量：硬下限由阶段一“必须推满 sink_floor”保证，
        # 完整容量让阶段二业务增量仍能到达汇点。
        self._add(g, self.t, self.tt, big, 0.0)
        self.sink_floor = sink_floor
        return g, sink_floor

    def _init_potential(self, g, source):
        """Bellman-Ford 求初始势能（网络无负环，V 轮收敛）。"""
        n = len(g)
        p = [0.0] * n
        for _ in range(n):
            changed = False
            for u in range(n):
                for e in g[u]:
                    if e.cap > EPS and p[u] + e.cost < p[e.to]:
                        p[e.to] = p[u] + e.cost
                        changed = True
            if not changed:
                break
        return p

    def _augment(self, g, s, t, limit, potential):
        """势能 Dijkstra 最小费用增广，返回实推流量。"""
        n = len(g)
        pushed = 0.0
        while pushed < limit - EPS:
            dist = [float("inf")] * n
            pv = [-1] * n
            pe = [-1] * n
            dist[s] = 0.0
            heap = [(0.0, s)]
            while heap:
                d, u = heapq.heappop(heap)
                if d > dist[u]:
                    continue
                for i, e in enumerate(g[u]):
                    if e.cap <= EPS:
                        continue
                    rc = e.cost + potential[u] - potential[e.to]
                    nd = d + rc
                    if nd < dist[e.to]:
                        dist[e.to] = nd
                        pv[e.to] = u
                        pe[e.to] = i
                        heapq.heappush(heap, (nd, e.to))
            if dist[t] == float("inf"):
                break
            for v in range(n):
                if dist[v] < float("inf"):
                    potential[v] += dist[v]
            add = limit - pushed
            v = t
            while v != s:
                add = min(add, g[pv[v]][pe[v]].cap)
                v = pv[v]
            if add <= EPS:
                break
            v = t
            while v != s:
                u = pv[v]
                e = g[u][pe[v]]
                e.cap -= add
                g[v][e.rev].cap += add
                v = u
            pushed += add
        return pushed

    def solve(self, sense):
        # 显式上界预检：下界合计若超过段/单上限，任何分配都不可能成立
        base_total = sum(b["low"] for b in self.boxes)
        seg_lo, seg_hi = self.seg_bounds
        if base_total > seg_hi + EPS:
            raise Infeasible(
                f"各箱重量下界合计 {base_total:.2f} 超过段上限 {seg_hi:.2f}")
        if self.apply_order_bound and base_total > self.order_bounds[1] + EPS:
            raise Infeasible(
                f"各箱重量下界合计 {base_total:.2f} 超过整单上限 "
                f"{self.order_bounds[1]:.2f}")

        g, sink_floor = self._build(sense)
        supplied = base_total
        # 阶段一：把超源的下界重量全部最小费用送到汇，完成定向与可行性验证
        potential = self._init_potential(g, self.ss)
        got = self._augment(g, self.ss, self.tt, supplied, potential)
        if got + EPS < min(supplied, sink_floor):
            raise Infeasible("箱/格/段/单重量约束无法同时成立")
        # 阶段二：真实源增量。总量下限 = max(下界合计, 硬下限)，
        # 已通过阶段一满足下界合计；min 只补到硬下限缺口，max 推到箱上界。
        need_min = max(0.0, sink_floor - supplied)
        room = sum(b["high"] - b["low"] for b in self.boxes)
        target = need_min if sense == "min" else room
        self._augment(g, self.s, self.tt, target, potential)
        return self._read(g)

    def _read(self, g):
        alloc = [[0.0 for _ in self.cell_list] for _ in self.boxes]
        for (bi, cell), edge in self.choice_edges.items():
            used = edge.initial_cap - edge.cap
            if used > EPS:
                alloc[bi][self.cell_list.index(cell)] += used
        weights = [sum(row) for row in alloc]
        value = 0.0
        for bi, b in enumerate(self.boxes):
            for ci, cell in enumerate(self.cell_list):
                if alloc[bi][ci] > EPS and cell in b["prices"]:
                    value += alloc[bi][ci] * b["prices"][cell]
        return value, weights, alloc
