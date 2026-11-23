"""命令行入口：assess / plan / farmer / finance。

从项目根目录运行，仅依赖标准库：
    python3 -m settle.cli assess 资料/演练.json
    python3 -m settle.cli farmer 资料/演练.json D1 --alt new
    python3 -m settle.cli plan 资料/演练.json
    python3 -m settle.cli finance 资料/演练.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .feasible import assess_order
from .ingest import load_packet
from .pools import Ledger, plan_settlement
from .report import farmer_report, finance_report


def _all_assessments(result, alt_ids):
    return {oid: assess_order(result, oid, alt_ids) for oid in result.orders}


def main(argv=None):
    parser = argparse.ArgumentParser(description="果品换约分档分段结算")
    parser.add_argument("command", choices=["assess", "plan", "farmer", "finance"])
    parser.add_argument("path", type=Path)
    parser.add_argument("order_id", nargs="?")
    parser.add_argument("--alt", action="append", default=[],
                        help="同时计算换约价差，可重复")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    result = load_packet(args.path)
    ledger = Ledger.from_packet(result.packet)
    assessments = _all_assessments(result, args.alt)

    if args.command == "assess":
        payload = {
            oid: {
                "contract": a.contract_id,
                "payable_low": a.payable_low,
                "payable_high": a.payable_high,
                "certain_amount": a.certain_amount,
                "blocked": [
                    {"segment": b.key[1], "reasons": b.reasons,
                     "reviewable_known": b.reviewable_known}
                    for b in a.blocked],
                "alternative": a.alternative,
            }
            for oid, a in assessments.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.command == "farmer":
        if not args.order_id:
            parser.error("farmer 需要指定交货单编号")
        print(farmer_report(result, args.order_id, args.alt))
        return

    plans = plan_settlement(result, assessments, ledger)
    if args.command == "plan":
        payload = {
            pid: {
                "advance_remaining": p.advance_remaining,
                "hold_remaining": p.hold_remaining,
                "orders": [{
                    "order_id": o.order_id,
                    "certain": o.certain,
                    "new_signable": o.new_signable,
                    "prepaid_excess": o.prepaid_excess,
                    "hold_request": o.hold_request,
                    "hold_granted": o.hold_granted,
                    "hold_shortfall": o.hold_shortfall,
                } for o in p.orders],
            } for pid, p in plans.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(finance_report(result, plans, ledger))


if __name__ == "__main__":
    main()
