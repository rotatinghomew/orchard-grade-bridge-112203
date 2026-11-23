"""面向果农与财务的两套可读口径。"""
from __future__ import annotations

from .feasible import OrderAssessment, assess_order
from .pools import PoolPlan


def _money(v):
    return "—" if v is None else f"{v:,.2f} 元"


def farmer_report(result, order_id: str, alternative_ids=None) -> str:
    """果农视角：哪些箱已形成确定收入、哪段只是估计、复核能解开多少。"""
    assessment = assess_order(result, order_id, alternative_ids)
    order = result.orders[order_id]
    lines = [
        f"交货单 {order_id}（{order['date']}，合同 {assessment.contract_id}）",
        f"整单称重范围 {order['net_kg_low']}~{order['net_kg_high']}kg；"
        f"整单总量约束{'已全量核验' if assessment.order_total_verified else '因隔离段未参与核验'}",
        f"确定收入（已可签发部分）：{_money(assessment.certain_amount)}",
        f"整单应付可行区间：{_money(assessment.payable_low)} ~ {_money(assessment.payable_high)}",
        f"估计差额（待证据）：{_money(max(0.0, assessment.payable_high - assessment.certain_amount))}",
        "",
    ]
    for seg in assessment.segments:
        head = f"段 {seg.key[1]}（上报形态：{seg.classification}，共 {seg.grid_total} 箱；逐箱 {seg.known_count}，匿名 {seg.anonymous_count}）"
        lines.append(head)
        if not seg.ok:
            lines.append("  ✗ 不能结算，原因：")
            for reason in seg.blocked.reasons:
                lines.append(f"    - {reason}")
            if seg.blocked.reviewable_known:
                lines.append("  其中已可独立审查的已知箱：")
                for item in seg.blocked.reviewable_known:
                    lines.append(
                        f"    · {item['box']} 格 {item['cell']} "
                        f"{item['kg']}kg × {item['price']} = {_money(item['amount'])}")
            continue
        lines.append(f"  段应付区间：{_money(seg.payable_low)} ~ {_money(seg.payable_high)}")
        lines.append("  各等级箱数可行范围：")
        for cell, (lo, hi) in sorted(seg.grade_ranges.items()):
            lines.append(f"    · 新合同格 {cell}：{lo} ~ {hi} 箱")
        if seg.uncertain_known:
            lines.append("  待下一次过线复核解开的已知箱：")
            for item in seg.uncertain_known:
                pr = item["price_range"]
                lines.append(
                    f"    · {item['box']}（复核字段：{','.join(item['checked']) or '无'}）"
                    f"单价 {pr[0]}~{pr[1]} 元/kg，{item['kg']}kg；{item['unlock_note']}")
        if seg.anonymous_count:
            lines.append(
                f"  匿名 {seg.anonymous_count} 箱仅有旧交叉表分档，"
                "金额随联合分档与称重分配在上述区间内浮动")
        lines.append("")

    for alt_id, alt in assessment.alternative.items():
        lines.append(
            f"若改用合同 {alt_id}：金额 {_money(alt['amount_b_low'])} ~ "
            f"{_money(alt['amount_b_high'])}，与现合同价差 "
            f"{_money(alt['diff_low'])} ~ {_money(alt['diff_high'])}")

    # 剩余证据差：逐箱复核能把多少估计金额转为确定收入
    unlock_boxes = [u for seg in assessment.segments if seg.ok
                    for u in seg.uncertain_known]
    anon_total = sum(seg.anonymous_count for seg in assessment.segments
                     if seg.ok)
    lines.append("")
    lines.append("剩余证据差：")
    lines.append(
        f"  待过线复核的跨档已知箱 {len(unlock_boxes)} 个；"
        f"仅有交叉表分档的匿名箱 {anon_total} 个")
    if unlock_boxes:
        spread = sum((u["price_range"][1] - u["price_range"][0]) * u["kg"]
                     for u in unlock_boxes)
        lines.append(
            f"  下一次过线复核可立即收敛的单价浮动金额约 {_money(spread)}")
    lines.append(
        "  补齐匿名箱逐箱读数后，估计差额可转为确定收入并重新参与额度排序")
    return "\n".join(lines)


def finance_report(result, plans: dict[str, PoolPlan], ledger) -> str:
    """财务视角：双额度占用、可签/待资/证据差、台账支付事实。"""
    lines = ["额度池结算安排", "=" * 24]
    for plan in plans.values():
        lines.append(
            f"池 {plan.pool}：确定款额度 {plan.advance_ceiling} "
            f"（已占 {plan.advance_used_before:.2f}，剩余 {plan.advance_remaining:.2f}）；"
            f"准备额额度 {plan.hold_ceiling} "
            f"（已占 {plan.hold_used_before:.2f}，剩余 {plan.hold_remaining:.2f}）")
        for op in plan.orders:
            lines.append(
                f"  {op.rank[0] + 1}. {op.order_id} 确定款 {_money(op.certain)}，"
                f"已付 {_money(op.already_paid)}，已签未付 {_money(op.already_signed)}，"
                f"本次可签 {_money(op.new_signable)}，"
                f"争议待资申请 {_money(op.hold_request)}（批准 {_money(op.hold_granted)}，"
                f"缺口 {_money(op.hold_shortfall)}）")
            if op.prepaid_excess > 0:
                lines.append(
                    f"     早期预付超出可核确定款 {_money(op.prepaid_excess)}，"
                    "列待追回（不改当日支付事实，需另挂原凭证）")
            if op.blocked:
                for blocked in op.blocked:
                    lines.append(f"     隔离段 {blocked.key[1]}：")
                    for reason in blocked.reasons:
                        lines.append(f"       - {reason}")
        lines.append("")

    lines.append("支付台账（当日事实不改，补付/追回另列）")
    lines.append("-" * 24)
    for entry in ledger.entries:
        lines.append(
            f"{entry.date} {entry.shipment} {entry.stage} {entry.amount:,.2f} 元 "
            f"凭证 {entry.receipt}" + (f"（{entry.note}）" if entry.note else ""))
    return "\n".join(lines)
