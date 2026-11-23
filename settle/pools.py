"""待结额度池与支付台账。

两本账分开记：
- advance（确定款）：已付 + 已签 + 本次新增，受 advance_ceiling 限制；
- hold（争议差额准备额）：仍未达成一致的追加差额，受 hold_ceiling 限制。

先保护已有签发；新增按 policy.priority 的交货日、编号先后。
已经付出的款项不可改，只能另持有原凭证的补付/追回；撤回只释放未付款项。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .feasible import OrderAssessment
from .lp import EPS


@dataclass
class LedgerEntry:
    shipment: str
    amount: float
    stage: str                 # paid / signed / supplement / clawback
    receipt: str
    date: str
    note: str = ""


@dataclass
class Ledger:
    entries: list[LedgerEntry] = field(default_factory=list)

    @classmethod
    def from_packet(cls, packet: dict) -> "Ledger":
        ledger = cls()
        for row in packet["payments"]:
            ledger.entries.append(LedgerEntry(
                shipment=row["shipment"], amount=float(row["amount"]),
                stage=row["stage"], receipt=row["receipt"], date=row["date"]))
        return ledger

    def paid_total(self, shipment: str) -> float:
        return sum(e.amount for e in self.entries
                   if e.shipment == shipment and e.stage == "paid")

    def signed_total(self, shipment: str) -> float:
        return sum(e.amount for e in self.entries
                   if e.shipment == shipment and e.stage == "signed")

    def pool_usage(self, pool_orders: set[str]) -> tuple[float, float]:
        """返回 (确定款占用, 准备额占用)。paid/signed/supplement 计入确定款。"""
        advance = sum(e.amount for e in self.entries
                      if e.shipment in pool_orders
                      and e.stage in ("paid", "signed", "supplement"))
        hold = sum(e.amount for e in self.entries
                   if e.shipment in pool_orders and e.stage == "hold")
        return advance, hold

    def confirm(self, receipt: str):
        """已签款项确认出金：signed -> paid，当日支付事实之外不改金额。"""
        for entry in self.entries:
            if entry.receipt == receipt and entry.stage == "signed":
                entry.stage = "paid"
                entry.note = (entry.note + "；" if entry.note else "") + "已确认出金"
                return entry
        raise KeyError(f"凭证 {receipt} 没有可确认的已签款项")

    def withdraw(self, receipt: str):
        """撤回：仅释放未付款的 signed 条目；paid 不可撤回。"""
        for entry in self.entries:
            if entry.receipt == receipt:
                if entry.stage == "signed":
                    entry.stage = "withdrawn"
                    entry.note = (entry.note + "；" if entry.note else "") + "已撤回"
                    return entry
                raise ValueError(f"凭证 {receipt} 阶段为 {entry.stage}，不能撤回")
        raise KeyError(f"凭证 {receipt} 不存在")

    def supplement(self, shipment: str, amount: float, receipt: str, date: str,
                   note: str = ""):
        """补付：必须挂原凭证链，另列条目，不改既有支付事实。"""
        if amount <= 0:
            raise ValueError("补付金额必须为正")
        entry = LedgerEntry(shipment, float(amount), "supplement", receipt, date,
                            note or "凭原凭证补付")
        self.entries.append(entry)
        return entry

    def clawback(self, shipment: str, amount: float, receipt: str, date: str,
                 note: str = ""):
        """追回：另列负向条目，不动当日支付记录。"""
        if amount <= 0:
            raise ValueError("追回金额必须为正")
        entry = LedgerEntry(shipment, -float(amount), "clawback", receipt, date,
                            note or "凭原凭证追回")
        self.entries.append(entry)
        return entry


@dataclass
class OrderPlan:
    order_id: str
    pool: str
    certain: float
    already_paid: float
    already_signed: float
    new_signable: float
    prepaid_excess: float
    hold_request: float
    hold_granted: float
    hold_shortfall: float
    blocked: list
    rank: tuple


@dataclass
class PoolPlan:
    pool: str
    advance_ceiling: float
    hold_ceiling: float
    advance_used_before: float
    hold_used_before: float
    orders: list[OrderPlan]
    advance_remaining: float
    hold_remaining: float


def _priority_key(order: dict, policy: dict):
    keys = policy.get("priority", ["date", "id"])
    return tuple(order[k] for k in keys)


def plan_settlement(result, assessments: dict[str, OrderAssessment],
                    ledger: Ledger) -> dict[str, PoolPlan]:
    """按公开优先条件给出可签分段结算、待资金额与剩余证据差。

    确定款只能来自可核段；存在不能一致段的订单不使用该段估值签发，
    但其已知款仍随可核段独立审查。
    """
    orders_by_pool: dict[str, list[dict]] = {}
    for order in result.orders.values():
        orders_by_pool.setdefault(order["pool"], []).append(order)

    plans: dict[str, PoolPlan] = {}
    for pool_id, orders in orders_by_pool.items():
        pool_cfg = result.pools[pool_id]
        pool_order_ids = {o["id"] for o in orders}
        advance_used, hold_used = ledger.pool_usage(pool_order_ids)
        advance_room = pool_cfg["advance_ceiling"] - advance_used
        hold_room = pool_cfg["hold_ceiling"] - hold_used
        if advance_room < -EPS or hold_room < -EPS:
            # 已有签发受保护：超额只报警，不冲销旧条目
            pass

        order_plans: list[OrderPlan] = []
        ranked = sorted(orders, key=lambda o: _priority_key(o, result.policy))
        for rank, order in enumerate(ranked):
            assessment = assessments[order["id"]]
            paid = ledger.paid_total(order["id"])
            signed = ledger.signed_total(order["id"])
            certain = assessment.certain_amount
            covered = paid + signed
            # 已付/已签受保护；新增只补确定款尚未覆盖的部分
            wanted_new = max(0.0, certain - covered)
            new_signable = min(wanted_new, max(0.0, advance_room))
            advance_room -= new_signable
            # 早期预付若超过当前可核确定款，差额列待追回，不冲销当日支付
            prepaid_excess = max(0.0, covered - certain)

            # 争议差额：整单可行上限与确定款之差（仅对可核段）
            dispute = max(0.0, assessment.payable_high - certain)
            hold_grant = min(dispute, max(0.0, hold_room))
            hold_room -= hold_grant

            order_plans.append(OrderPlan(
                order_id=order["id"], pool=pool_id, certain=certain,
                already_paid=paid, already_signed=signed,
                new_signable=_round(new_signable),
                prepaid_excess=_round(prepaid_excess),
                hold_request=_round(dispute),
                hold_granted=_round(hold_grant),
                hold_shortfall=_round(dispute - hold_grant),
                blocked=assessment.blocked,
                rank=(rank, *_priority_key(order, result.policy))))
        plans[pool_id] = PoolPlan(
            pool=pool_id,
            advance_ceiling=pool_cfg["advance_ceiling"],
            hold_ceiling=pool_cfg["hold_ceiling"],
            advance_used_before=advance_used, hold_used_before=hold_used,
            orders=order_plans,
            advance_remaining=_round(max(0.0, advance_room)),
            hold_remaining=_round(max(0.0, hold_room)))
    return plans


def _round(value: float) -> float:
    return round(value + 0.0, 2)
