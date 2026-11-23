"""资料接收与识别：重复上报、补齐、独立段，以及 revision 替换与复核合并。

段的身份是 `(shipment, segment)`：
- 同段交叉表按到件时间与递增 revision 替换，旧版留痕但不参与约束；
- 同段逐箱读数是段内细化，不另加数量；
- 新交叉表同时约束已经明确的箱；
- checks 的区间替代单点读数，但不改变箱的其他字段。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .grades import GradeScheme


class IngestionError(ValueError):
    """资料包结构或引用不满足公开约定。"""


def read_packet(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or any(
        not isinstance(value, (list, dict)) for value in data.values()
    ):
        raise ValueError("资料包必须是由记录列表或参数对象组成的 JSON 对象")
    required = {"contracts", "orders", "grids", "boxes", "pools", "payments",
                "segment_limits", "policy"}
    missing = required - set(data)
    if missing:
        raise IngestionError(f"资料包缺少必要资料：{sorted(missing)}")
    return data


@dataclass
class BoxEvidence:
    box: str
    shipment: str
    segment: str
    diameter: tuple[float, float]          # 复核后始终表示为闭区间
    color: tuple[float, float]
    kg: float
    loaded: str
    check_fields: tuple[str, ...] = ()
    has_measured_kg: bool = True


@dataclass
class SegmentView:
    shipment: str
    segment: str
    grid_cells: tuple[tuple[int, ...], ...]
    grid_revision: int
    grid_known: str
    boxes: dict[str, BoxEvidence] = field(default_factory=dict)
    kg_low: float | None = None
    kg_high: float | None = None
    superseded_grids: tuple[dict, ...] = ()


@dataclass
class IngestionResult:
    packet: dict
    schemes: dict[str, GradeScheme]
    orders: dict[str, dict]
    segments: dict[tuple[str, str], SegmentView]
    checks: list[dict]
    payments: list[dict]
    pools: dict[str, dict]
    policy: dict
    classifications: dict[tuple[str, str], str]


def load_packet(path: str | Path) -> IngestionResult:
    packet = read_packet(path)
    policy = packet["policy"]
    schemes = {
        row["id"]: GradeScheme.from_contract(row, policy)
        for row in packet["contracts"]
    }
    orders = {row["id"]: row for row in packet["orders"]}

    # ---- 交叉表：同段按 revision 取最新，旧版留痕 ----
    grid_views: dict[tuple[str, str], list[dict]] = {}
    for row in packet["grids"]:
        if row.get("passport") != "legacy":
            raise IngestionError(f"未知交叉表口径：{row.get('passport')}")
        grid_views.setdefault((row["shipment"], row["segment"]), []).append(row)

    segments: dict[tuple[str, str], SegmentView] = {}
    for key, rows in grid_views.items():
        rows = sorted(rows, key=lambda r: (r["known"], r["revision"]))
        latest = rows[-1]
        if any(r["revision"] >= latest["revision"] and r is not latest for r in rows):
            raise IngestionError(f"段 {key} 的 revision 无法确定唯一最新版")
        segments[key] = SegmentView(
            shipment=key[0],
            segment=key[1],
            grid_cells=tuple(tuple(line) for line in latest["cells"]),
            grid_revision=latest["revision"],
            grid_known=latest["known"],
            superseded_grids=tuple(rows[:-1]),
        )

    # ---- 段称重范围 ----
    for row in packet["segment_limits"]:
        key = (row["shipment"], row["segment"])
        if key not in segments:
            raise IngestionError(f"称重范围引用了不存在的段：{key}")
        segments[key].kg_low = row["kg_low"]
        segments[key].kg_high = row["kg_high"]
    for view in segments.values():
        if view.kg_low is None:
            raise IngestionError(f"段 {(view.shipment, view.segment)} 缺少分段称重范围")

    # ---- 逐箱读数：段内细化，不另加数量；checks 以区间替代单点 ----
    check_index: dict[tuple[str, str], dict[str, dict]] = {}
    for row in packet.get("checks", []):
        check_index.setdefault((row["shipment"], row["segment"]), {})[row["box"]] = row

    for row in packet["boxes"]:
        key = (row["shipment"], row["segment"])
        if key not in segments:
            raise IngestionError(f"逐箱读数引用了不存在的段：{key}")
        check = check_index.get(key, {}).get(row["box"])
        checked = []
        d_low = d_high = row["diameter"]
        c_low = c_high = row["color"]
        if check and check["field"] == "diameter":
            d_low, d_high = check["low"], check["high"]
            checked.append("diameter")
        if check and check["field"] == "color":
            c_low, c_high = check["low"], check["high"]
            checked.append("color")
        kg = row.get("kg")
        if kg is None:
            kg = (policy["min_box_kg"] + policy["max_box_kg"]) / 2
        evidence = BoxEvidence(
            box=row["box"],
            shipment=row["shipment"],
            segment=row["segment"],
            diameter=(float(d_low), float(d_high)),
            color=(float(c_low), float(c_high)),
            kg=float(kg),
            loaded=row["loaded"],
            check_fields=tuple(checked),
            has_measured_kg="kg" in row,
        )
        if evidence.box in segments[key].boxes:
            raise IngestionError(f"箱 {evidence.box} 重复逐箱上报")
        segments[key].boxes[evidence.box] = evidence

    _validate_references(packet, orders, segments)
    classifications = classify_segments(packet, segments)
    return IngestionResult(
        packet=packet,
        schemes=schemes,
        orders=orders,
        segments=segments,
        checks=packet.get("checks", []),
        payments=packet["payments"],
        pools={row["pool"]: row for row in packet["pools"]},
        policy=policy,
        classifications=classifications,
    )


def _validate_references(packet, orders, segments):
    order_ids = set(orders)
    for key in segments:
        if key[0] not in order_ids:
            raise IngestionError(f"段 {key} 引用了不存在的交货单")
    for row in packet["payments"]:
        if row["shipment"] not in order_ids:
            raise IngestionError(f"款项引用了不存在的交货单：{row['shipment']}")
        if row["stage"] not in ("paid", "signed"):
            raise IngestionError(f"款项阶段非法：{row['stage']}")
    for row in packet["orders"]:
        if row["contract"] not in {c["id"] for c in packet["contracts"]}:
            raise IngestionError(f"订单 {row['id']} 的合同不存在")
        if row["pool"] not in {p["pool"] for p in packet["pools"]}:
            raise IngestionError(f"订单 {row['id']} 的额度池不存在")


def classify_segments(packet: dict, segments: dict[tuple[str, str], SegmentView]):
    """按资料形态识别每个段的上报性质。

    - duplicate：同段交叉表存在被替换的旧 revision（重复上报，新版覆盖）；
    - partial：同段除交叉表外还出现逐箱读数（对其中一部分的补齐细化）；
    - standalone：仅有单一版本交叉表、没有逐箱读数的独立一段货。
    """
    result = {}
    box_segments = {(b["shipment"], b["segment"]) for b in packet["boxes"]}
    for key, view in segments.items():
        if view.superseded_grids:
            result[key] = "duplicate"
        elif key in box_segments:
            result[key] = "partial"
        else:
            result[key] = "standalone"
    return result
