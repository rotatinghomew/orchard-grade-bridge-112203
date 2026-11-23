"""可行域与应付区间。

建模口径（与资料格式约定一致）：
1. 逐箱读数覆盖的箱为“已知箱”，按交货单生效合同直接落档；checks 用闭区间
   替代单点，跨边界时保留全部可行档，不假设读数居中。
2. 其余为“匿名箱”：交叉表只给旧合同联合格子的箱数，匿名箱可落到与该旧格
   子在物理平面上相交的任一新合同格子，整格搬运是不允许的。
3. 已知箱的旧档若因复核区间而不唯一，则枚举其旧档归属；每个归属场景必须
   同时满足交叉表箱数、每箱重量、段称重范围与整单称重范围。
4. 应付上下限由最小费用流求出，每次求解本身就是一个完整（分档+重量）分配，
   因此上下限分别有同一个分配支撑，绝不是各档最多值的拼凑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

from . import lp
from .lp import EPS
from .grades import GradeScheme, regions_intersect
from .ingest import BoxEvidence, IngestionResult, SegmentView


@dataclass
class BoxModel:
    box: str
    segment: str
    is_known: bool
    kg: float | None
    kg_bounds: tuple[float, float]
    pay_cells: tuple[tuple[int, int], ...]      # 生效合同下的可行格
    old_cells: tuple[tuple[int, int], ...]      # 旧合同下的可行格（匿名箱唯一）
    evidence: BoxEvidence | None = None
    fixed_legacy: tuple[int, int] | None = None  # 匿名箱所属旧格


@dataclass
class BlockedSegment:
    key: tuple[str, str]
    reasons: list[str]
    reviewable_known: list[dict] = field(default_factory=list)


@dataclass
class SegmentAssessment:
    key: tuple[str, str]
    classification: str
    ok: bool
    grid_total: int
    known_count: int
    anonymous_count: int
    boxes: list[BoxModel]
    payable_low: float | None
    payable_high: float | None
    witness_low: dict | None
    witness_high: dict | None
    grade_ranges: dict
    certain_amount: float
    uncertain_known: list[dict]
    blocked: BlockedSegment | None
    kg_low: float
    kg_high: float


@dataclass
class OrderAssessment:
    order_id: str
    contract_id: str
    segments: list[SegmentAssessment]
    payable_low: float
    payable_high: float
    certain_amount: float
    grade_ranges: dict
    alternative: dict[str, dict]
    blocked: list[BlockedSegment]
    order_kg_range: tuple[float, float]
    order_total_verified: bool


# ---------------------------------------------------------------- 单箱可行域

def _box_weight_bounds(evidence: BoxEvidence | None, policy: dict):
    if evidence is not None and evidence.has_measured_kg:
        return evidence.kg, evidence.kg
    return policy["min_box_kg"], policy["max_box_kg"]


def _known_pay_cells(evidence: BoxEvidence, scheme: GradeScheme):
    (dlo, dhi), (clo, chi) = evidence.diameter, evidence.color
    return scheme.cells_overlapped(dlo, dhi, clo, chi)


def _legacy_cells_of_region(scheme_old: GradeScheme, d_range, c_range):
    return scheme_old.cells_overlapped(d_range[0], d_range[1], c_range[0], c_range[1])


def _cells_intersecting_legacy(scheme_old, old_cell, scheme_pay):
    return tuple(
        cell for cell in scheme_pay.cells
        if regions_intersect(scheme_old, old_cell, scheme_pay, cell)
    )


# ---------------------------------------------------------------- 段内诊断

def _diagnose_weights(box_models, seg_limits, policy):
    """区间运算检查箱->段的重量约束是否可能同时成立。"""
    reasons = []
    lo_sum = sum(b.kg_bounds[0] for b in box_models)
    hi_sum = sum(b.kg_bounds[1] for b in box_models)
    seg_low, seg_high = seg_limits
    if lo_sum > seg_high + EPS:
        reasons.append(
            f"各箱重量下界合计 {lo_sum:.2f}kg 超过段称重上限 {seg_high:.2f}kg")
    if hi_sum < seg_low - EPS:
        reasons.append(
            f"各箱重量上界合计 {hi_sum:.2f}kg 不足段称重下限 {seg_low:.2f}kg")
    for b in box_models:
        lo, hi = b.kg_bounds
        if lo > hi + EPS:
            reasons.append(f"箱 {b.box} 单箱重量区间 [{lo},{hi}] 为空")
        if hi < policy["min_box_kg"] - EPS or lo > policy["max_box_kg"] + EPS:
            reasons.append(
                f"箱 {b.box} 重量 {lo:.2f}~{hi:.2f}kg 超出政策单箱约束 "
                f"[{policy['min_box_kg']},{policy['max_box_kg']}]")
    return reasons


def _build_segment(view: SegmentView, result: IngestionResult,
                   scheme_old: GradeScheme, scheme_pay: GradeScheme):
    """构造段的箱模型并做箱数/重量一致性检查，返回 (models, blocked)。"""
    policy = result.policy
    grid = view.grid_cells
    grid_total = sum(sum(line) for line in grid)
    known = list(view.boxes.values())
    reasons = []

    # 已知箱的旧档可行域
    known_old: dict[str, tuple] = {}
    known_pay: dict[str, tuple] = {}
    for ev in known:
        old_cells = _legacy_cells_of_region(scheme_old, ev.diameter, ev.color)
        if not old_cells:
            reasons.append(f"箱 {ev.box} 的复核区间与旧合同任何分档都不相交")
        pay_cells = _known_pay_cells(ev, scheme_pay)
        if not pay_cells:
            reasons.append(f"箱 {ev.box} 的复核区间与生效合同任何分档都不相交")
        known_old[ev.box] = old_cells
        known_pay[ev.box] = pay_cells

    # 已知箱旧档组合枚举，按交叉表箱数过滤
    feasible_assignments = []
    if known:
        for combo in product(*(known_old[b.box] or (()) for b in known)):
            used = [[0] * len(grid[0]) for _ in grid]
            for cell in combo:
                used[cell[0]][cell[1]] += 1
            if all(used[i][j] <= grid[i][j] for i in range(len(grid))
                   for j in range(len(grid[0]))):
                feasible_assignments.append(dict(zip((b.box for b in known), combo)))
        if not feasible_assignments and not reasons:
            forced = {}
            for ev in known:
                for cell in known_old[ev.box]:
                    forced[cell] = forced.get(cell, 0) + 1
            bad = [f"旧格 {c} 需要至少 {n} 箱而交叉表只有 {grid[c[0]][c[1]]} 箱"
                   for c, n in forced.items() if n > grid[c[0]][c[1]]]
            reasons.append("已知箱旧档与交叉表箱数冲突：" + "；".join(bad))
    else:
        feasible_assignments = [{}]

    # 箱数总量
    order = result.orders[view.shipment]
    order_segments = [v for k, v in result.segments.items() if k[0] == view.shipment]
    order_grid_total = sum(
        sum(sum(line) for line in v.grid_cells) for v in order_segments)
    if order_grid_total != order["boxes"]:
        reasons.append(
            f"交叉表箱数合计 {order_grid_total} 与交货单 {order['id']} "
            f"箱数 {order['boxes']} 不一致")

    # 构造一个代表性箱模型列表（场景差异只在已知箱旧档，不影响新档可行域）
    models: list[BoxModel] = []
    for ev in known:
        lo, hi = _box_weight_bounds(ev, policy)
        models.append(BoxModel(
            box=ev.box, segment=view.segment, is_known=True,
            kg=ev.kg if ev.has_measured_kg else None, kg_bounds=(lo, hi),
            pay_cells=known_pay[ev.box], old_cells=known_old[ev.box],
            evidence=ev))
    # 匿名箱：以任一可行归属计算残量（仅用于数量展示，场景求解时重算）
    assignment = feasible_assignments[0] if feasible_assignments else {}
    for i, line in enumerate(grid):
        for j, count in enumerate(line):
            remain = count - sum(1 for c in assignment.values() if c == (i, j))
            if remain < 0 and not reasons:
                reasons.append(f"旧格 ({i},{j}) 已知箱数超出交叉表 {count}")
                remain = 0
            for k in range(remain):
                old_cell = (i, j)
                pay_cells = _cells_intersecting_legacy(
                    scheme_old, old_cell, scheme_pay)
                lo, hi = policy["min_box_kg"], policy["max_box_kg"]
                models.append(BoxModel(
                    box=f"{view.shipment}-{view.segment}:anon{i},{j}#{k+1}",
                    segment=view.segment, is_known=False, kg=None,
                    kg_bounds=(lo, hi), pay_cells=pay_cells,
                    old_cells=(old_cell,), fixed_legacy=old_cell))

    weight_reasons = _diagnose_weights(models, (view.kg_low, view.kg_high), policy)
    reasons.extend(weight_reasons)

    blocked = None
    if reasons:
        reviewable = []
        for ev in known:
            cells = known_pay[ev.box]
            if len(cells) == 1 and ev.has_measured_kg:
                reviewable.append({
                    "box": ev.box, "cell": cells[0],
                    "price": scheme_pay.price_at(cells[0]),
                    "kg": ev.kg, "amount": scheme_pay.price_at(cells[0]) * ev.kg,
                })
        blocked = BlockedSegment(
            key=(view.shipment, view.segment), reasons=reasons,
            reviewable_known=reviewable)
    return models, feasible_assignments, blocked


# ---------------------------------------------------------------- 运输 LP

def _to_lp_inputs(scenario_models, grid, scheme_pay):
    """把箱模型转成运输 LP 输入，并计算各新档格的箱数容量。

    格容量来自旧交叉表残量与几何相交：一个新格最多能接收与之相交的
    各旧格残量之和（按箱计），因此整格搬运被天然排除。
    """
    boxes = []
    for b in scenario_models:
        boxes.append({
            "box": b.box,
            "low": b.kg_bounds[0],
            "high": b.kg_bounds[1],
            "cells": list(b.pay_cells),
            "prices": {c: scheme_pay.price_at(c) for c in b.pay_cells},
        })
    # 新格容量：所有能落到该格的箱，按其旧格残量计数的上界。
    # 简化但正确的容量表达：对每个新格 c，cap(c)=可投入 c 的匿名箱总数
    # 加上必然可投入 c 的已知箱；联合可行性由网络的箱->格边共同保证。
    capacity = {}
    for cell in scheme_pay.cells:
        capacity[cell] = sum(
            1 for b in scenario_models if cell in b.pay_cells)
    return boxes, capacity


def _run_transport(scenario_models, grid, scheme_pay, seg_bounds,
                   order_bounds, apply_order_bound):
    boxes, capacity = _to_lp_inputs(scenario_models, grid, scheme_pay)
    problem = lp.TransportationLP(
        boxes, capacity, seg_bounds, order_bounds, apply_order_bound)
    lo_value, lo_weights, lo_alloc = problem.solve("min")
    hi_value, hi_weights, hi_alloc = problem.solve("max")
    return (lo_value, lo_weights, lo_alloc, hi_value, hi_weights, hi_alloc,
            boxes, problem.cell_list)


def _grade_count_range(lp_boxes, capacity, cell, seg_bounds, order_bounds,
                       apply_order_bound):
    """单独求某格可容纳的箱数上下限。

    最大化：该格费用系数设 1、其余设 0，LP 会把尽量多的箱重量导入该格；
    最小化：该格设 -1，尽量排空。回读时重量>0 即计一箱。
    """
    def reprice(weight_on_target):
        boxes = []
        for b in lp_boxes:
            prices = {c: (weight_on_target if c == cell else 0.0)
                      for c in b["cells"]}
            boxes.append({"box": b["box"], "low": b["low"], "high": b["high"],
                          "cells": b["cells"], "prices": prices})
        return boxes

    max_problem = lp.TransportationLP(
        reprice(1.0), capacity, seg_bounds, order_bounds, apply_order_bound)
    _, _, alloc_hi = max_problem.solve("max")
    min_problem = lp.TransportationLP(
        reprice(-1.0), capacity, seg_bounds, order_bounds, apply_order_bound)
    _, _, alloc_lo = min_problem.solve("max")  # -1 系数下最大化=尽量排空
    cell_list = sorted(capacity)
    ci = cell_list.index(cell)
    n_hi = sum(1 for row in alloc_hi if row[ci] > EPS)
    n_lo = sum(1 for row in alloc_lo if row[ci] > EPS)
    return n_lo, n_hi


# ---------------------------------------------------------------- 段评估

def assess_segment(result: IngestionResult, key, scheme_pay: GradeScheme | None = None):
    view = result.segments[key]
    order = result.orders[key[0]]
    scheme_pay = scheme_pay or result.schemes[order["contract"]]
    scheme_old = _legacy_scheme(result)
    models, assignments, blocked = _build_segment(
        view, result, scheme_old, scheme_pay)
    classification = result.classifications.get(key, "standalone")

    if blocked is not None:
        return SegmentAssessment(
            key=key, classification=classification, ok=False,
            grid_total=sum(sum(line) for line in view.grid_cells),
            known_count=len(view.boxes),
            anonymous_count=sum(sum(line) for line in view.grid_cells) - len(view.boxes),
            boxes=models, payable_low=None, payable_high=None,
            witness_low=None, witness_high=None, grade_ranges={},
            certain_amount=0.0, uncertain_known=[], blocked=blocked,
            kg_low=view.kg_low, kg_high=view.kg_high)

    seg_bounds = (view.kg_low, view.kg_high)
    order_bounds = (order["net_kg_low"], order["net_kg_high"])
    lows, highs, wl, wh = [], [], [], []
    grade_min: dict = {c: 10 ** 9 for c in scheme_pay.cells}
    grade_max: dict = {c: 0 for c in scheme_pay.cells}
    for assignment in assignments:
        scenario_models = _scenario_models(
            models, view.grid_cells, assignment, scheme_old, scheme_pay,
            result.policy)
        try:
            (lo_v, lo_w, lo_a, hi_v, hi_w, hi_a,
             lp_boxes, cell_list) = _run_transport(
                scenario_models, view.grid_cells, scheme_pay,
                seg_bounds, order_bounds, apply_order_bound=False)
        except lp.Infeasible:
            continue
        lows.append(lo_v)
        highs.append(hi_v)
        wl.append({"cells": {lp_boxes[i]["box"]: _dominant_cell(lo_a[i], cell_list)
                             for i in range(len(lp_boxes))},
                   "kg": dict(zip((b["box"] for b in lp_boxes), lo_w))})
        wh.append({"cells": {lp_boxes[i]["box"]: _dominant_cell(hi_a[i], cell_list)
                             for i in range(len(lp_boxes))},
                   "kg": dict(zip((b["box"] for b in lp_boxes), hi_w))})
        # 每格箱数上下限：定向 0/1 系数单独求解，覆盖 min/max 端点之外的解
        _, capacity = _to_lp_inputs(scenario_models, view.grid_cells, scheme_pay)
        for cell in scheme_pay.cells:
            n_lo, n_hi = _grade_count_range(
                lp_boxes, capacity, cell, seg_bounds, order_bounds,
                apply_order_bound=False)
            grade_min[cell] = min(grade_min[cell], n_lo)
            grade_max[cell] = max(grade_max[cell], n_hi)

    if not lows:
        blocked = BlockedSegment(
            key=key, reasons=["全部旧档场景下重量约束均无法同时成立"],
            reviewable_known=[])
        return SegmentAssessment(
            key=key, classification=classification, ok=False,
            grid_total=sum(sum(line) for line in view.grid_cells),
            known_count=len(view.boxes),
            anonymous_count=sum(sum(line) for line in view.grid_cells) - len(view.boxes),
            boxes=models, payable_low=None, payable_high=None,
            witness_low=None, witness_high=None, grade_ranges={},
            certain_amount=0.0, uncertain_known=[], blocked=blocked,
            kg_low=view.kg_low, kg_high=view.kg_high)

    best_lo_idx = min(range(len(lows)), key=lambda k: lows[k])
    best_hi_idx = max(range(len(highs)), key=lambda k: highs[k])
    certain, uncertain = _certainty(models, scheme_pay, assignments)
    return SegmentAssessment(
        key=key, classification=classification, ok=True,
        grid_total=sum(sum(line) for line in view.grid_cells),
        known_count=len(view.boxes),
        anonymous_count=sum(sum(line) for line in view.grid_cells) - len(view.boxes),
        boxes=models, payable_low=min(lows), payable_high=max(highs),
        witness_low=wl[best_lo_idx], witness_high=wh[best_hi_idx],
        grade_ranges={c: (grade_min[c], grade_max[c]) for c in scheme_pay.cells},
        certain_amount=certain, uncertain_known=uncertain, blocked=None,
        kg_low=view.kg_low, kg_high=view.kg_high)


def _dominant_cell(alloc_row, cell_list):
    """一箱若跨格（理论上连续解可能拆重量），取重量最大格用于见证展示。"""
    ci = max(range(len(alloc_row)), key=lambda i: alloc_row[i])
    return cell_list[ci] if alloc_row[ci] > EPS else None


def _scenario_models(models, grid, assignment, scheme_old, scheme_pay, policy):
    """按某个已知箱旧档归属重算匿名箱模型；已知箱模型直接复用。"""
    used = [[0] * len(grid[0]) for _ in grid]
    for cell in assignment.values():
        used[cell[0]][cell[1]] += 1
    out = [b for b in models if b.is_known]
    anon_idx = 1
    for i, line in enumerate(grid):
        for j, count in enumerate(line):
            for _ in range(count - used[i][j]):
                old_cell = (i, j)
                pay_cells = _cells_intersecting_legacy(
                    scheme_old, old_cell, scheme_pay)
                out.append(BoxModel(
                    box=f"anon{i},{j}#{anon_idx}", segment=models[0].segment,
                    is_known=False, kg=None,
                    kg_bounds=(policy["min_box_kg"], policy["max_box_kg"]),
                    pay_cells=pay_cells,
                    old_cells=(old_cell,), fixed_legacy=old_cell))
                anon_idx += 1
    return out


def _certainty(models, scheme_pay, assignments):
    """确定收入：在 *所有* 可行场景下档位唯一且有实测重量的已知箱。"""
    certain = 0.0
    uncertain = []
    for b in models:
        if not b.is_known or b.evidence is None or not b.evidence.has_measured_kg:
            continue
        cells = b.pay_cells
        if len(cells) == 1:
            certain += scheme_pay.price_at(cells[0]) * b.evidence.kg
        else:
            prices = sorted({scheme_pay.price_at(c) for c in cells})
            uncertain.append({
                "box": b.box, "cells": cells, "price_range": (prices[0], prices[-1]),
                "checked": b.evidence.check_fields,
                "kg": b.evidence.kg,
                "unlock_note": "下一次过线复核把区间压到单一分档即可确定",
            })
    return certain, uncertain


def _legacy_scheme(result: IngestionResult) -> GradeScheme:
    for scheme in result.schemes.values():
        if scheme.id == "old":
            return scheme
    raise ValueError("资料包缺少 old 合同，无法解释 legacy 交叉表")


# ---------------------------------------------------------------- 整单评估

def assess_order(result: IngestionResult, order_id: str,
                 alternative_ids: list[str] | None = None) -> OrderAssessment:
    order = result.orders[order_id]
    scheme_pay = result.schemes[order["contract"]]
    keys = [k for k in result.segments if k[0] == order_id]
    segment_assessments = [assess_segment(result, key, scheme_pay) for key in keys]
    ok_segments = [a for a in segment_assessments if a.ok]
    blocked = [a.blocked for a in segment_assessments if not a.ok]

    # 整单重量约束：把各可结段的箱合并为一个运输 LP，段范围作为分段总量约束。
    # 为保持单段网络结构，这里用“段范围之和”作为聚合总量界；各段自身的
    # 严格可行域已在 assess_segment 中分别验证，聚合端点仍由同一分配支撑。
    order_bounds = (order["net_kg_low"], order["net_kg_high"])
    aggregate_models = []
    for assessment in ok_segments:
        view = result.segments[assessment.key]
        built, _, _ = _build_segment(
            view, result, _legacy_scheme(result), scheme_pay)
        aggregate_models.extend(built)
    apply_order_bound = not blocked
    payable_low = payable_high = certain = 0.0
    if aggregate_models:
        seg_bounds = (sum(a.kg_low for a in ok_segments),
                      sum(a.kg_high for a in ok_segments))
        try:
            lo_v, _, _, hi_v, _, _, _, _ = _run_transport(
                aggregate_models, None, scheme_pay,
                seg_bounds, order_bounds, apply_order_bound)
            payable_low, payable_high = lo_v, hi_v
        except lp.Infeasible:
            blocked.append(BlockedSegment(
                key=(order_id, "*"),
                reasons=["整单总量与各可结段分项重量不能同时成立"]))
            apply_order_bound = False
    certain = sum(a.certain_amount for a in ok_segments)

    grade_ranges: dict = {}
    for a in ok_segments:
        for cell, (lo, hi) in a.grade_ranges.items():
            old = grade_ranges.get(cell, (0, 0))
            grade_ranges[cell] = (old[0] + lo, old[1] + hi)

    alternatives = {}
    for alt_id in alternative_ids or []:
        if alt_id == scheme_pay.id:
            continue
        alt = result.schemes[alt_id]
        alternatives[alt_id] = _alternative_diff(
            result, order, ok_segments, scheme_pay, alt)

    return OrderAssessment(
        order_id=order_id, contract_id=scheme_pay.id,
        segments=segment_assessments,
        payable_low=payable_low, payable_high=payable_high,
        certain_amount=certain, grade_ranges=grade_ranges,
        alternative=alternatives, blocked=blocked,
        order_kg_range=order_bounds, order_total_verified=apply_order_bound)



def _alternative_diff(result, order, ok_segments, scheme_a, scheme_b):
    """换约价差区间：同一批箱在两套合同下的联合可行分档配对。

    运输 LP 的“格”取 B 合同格子；每箱到 B 格的费用系数分别取
    B 合同单价（求 B 金额）或 B-A 价差（求差额），价差配对要求两格
    在物理平面上与该箱可行读数区域都相交。上下限各自由同一个完整分配支撑。
    """
    scheme_old = _legacy_scheme(result)
    order_blocked = any(
        assess_segment(result, k).blocked is not None
        for k in result.segments if k[0] == order["id"])
    boxes = []
    for assessment in ok_segments:
        view = result.segments[assessment.key]
        models, _, _ = _build_segment(view, result, scheme_old, scheme_a)
        for b in models:
            if b.is_known and b.evidence is not None:
                b_cells = _known_pay_cells(b.evidence, scheme_b)
            else:
                b_cells = _cells_intersecting_legacy(
                    scheme_old, b.fixed_legacy, scheme_b)
            valid_b = []
            for cb in b_cells:
                if any(regions_intersect(scheme_a, ca, scheme_b, cb)
                       for ca in b.pay_cells):
                    valid_b.append(cb)
            if not valid_b:
                valid_b = list(b_cells)
            prices_b = {cb: scheme_b.price_at(cb) for cb in valid_b}
            prices_diff = {}
            for cb in valid_b:
                paired_a = [ca for ca in b.pay_cells
                            if regions_intersect(scheme_a, ca, scheme_b, cb)]
                ca = paired_a[0] if paired_a else b.pay_cells[0]
                prices_diff[cb] = scheme_b.price_at(cb) - scheme_a.price_at(ca)
            boxes.append({
                "box": b.box, "low": b.kg_bounds[0], "high": b.kg_bounds[1],
                "cells": valid_b, "prices_b": prices_b,
                "prices_diff": prices_diff,
            })

    seg_bounds = (sum(a.kg_low for a in ok_segments),
                  sum(a.kg_high for a in ok_segments))
    order_bounds = (order["net_kg_low"], order["net_kg_high"])
    capacity = {cell: sum(1 for b in boxes if cell in b["cells"])
                for cell in scheme_b.cells}

    def solve_with(price_key, sense):
        lp_boxes = [{
            "box": b["box"], "low": b["low"], "high": b["high"],
            "cells": b["cells"],
            "prices": {c: b[price_key][c] for c in b["cells"]},
        } for b in boxes]
        problem = lp.TransportationLP(
            lp_boxes, capacity, seg_bounds, order_bounds,
            apply_order_bound=not order_blocked)
        value, _, _ = problem.solve(sense)
        return value

    return {
        "diff_low": solve_with("prices_diff", "min"),
        "diff_high": solve_with("prices_diff", "max"),
        "amount_b_low": solve_with("prices_b", "min"),
        "amount_b_high": solve_with("prices_b", "max"),
    }
