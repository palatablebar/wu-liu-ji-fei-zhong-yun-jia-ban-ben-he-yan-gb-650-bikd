"""尾差规则：上界、方向、归属与可解释文本。

核心立场：逐票已核金额是确认过的账，不因为月结总额与控制数差几分钱去改动任何一票。
差额只作为承运商月结层的一条独立尾差，注明方向与上界；超出上界就不是取整尾差，
必须停下来复核，不能硬塞。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from app import config
from app.modules.m5_charges.money import q2

TAIL_ROUNDING = "TAIL_ROUNDING"
TAIL_UNEXPLAINED = "TAIL_UNEXPLAINED"

DIRECTION_NONE = "无尾差"
DIRECTION_UP = "补收"
DIRECTION_DOWN = "冲减"

STATUS_READY = "可确认"
STATUS_CONFIRMED = "已确认"
STATUS_DRIFT = "已确认（数据已变）"
STATUS_REVIEW = "需复核"
STATUS_NO_CONTROL = "缺控制数"
STATUS_NO_BILLS = "无计费票"


def tail_bound(bill_count: int) -> Decimal:
    """尾差上界：每票取整最多差一分，再加控制数自身半分，向上取到分。"""
    per_bill = Decimal(bill_count) * config.TAIL_PER_BILL
    raw = per_bill + config.TAIL_CONTROL_HALF
    return raw.quantize(config.MONEY_QUANT, rounding=ROUND_CEILING)


def tail_of(control_total: Decimal, billed_total: Decimal) -> Decimal:
    return q2(Decimal(control_total) - Decimal(billed_total))


def direction_of(tail: Decimal) -> str:
    if tail > 0:
        return DIRECTION_UP
    if tail < 0:
        return DIRECTION_DOWN
    return DIRECTION_NONE


def explain(tail: Decimal, bill_count: int, bound: Decimal, control_total: Decimal,
            billed_total: Decimal) -> tuple[str, str]:
    """返回 (说明文本, 结论码)。结论码只用 TAIL_ROUNDING 或 TAIL_UNEXPLAINED。"""
    if tail == 0:
        return (
            f"{bill_count} 票逐票合计 {billed_total} 元与控制数 {control_total} 元相等，"
            f"无尾差；上界 {bound} 元。",
            TAIL_ROUNDING,
        )
    if abs(tail) <= bound:
        return (
            f"{bill_count} 票逐票合计 {billed_total} 元，控制数 {control_total} 元，"
            f"尾差 {tail:+} 元（{direction_of(tail)}）。上界 = {bill_count} 票 × 0.01 元"
            f" + 控制数半分 0.005 元，向上取到分 = {bound} 元；尾差落在上界内，"
            f"归属承运商月结取整尾差，不摊回任何一票。",
            TAIL_ROUNDING,
        )
    return (
        f"{bill_count} 票逐票合计 {billed_total} 元，控制数 {control_total} 元，"
        f"尾差 {tail:+} 元，超出取整上界 {bound} 元；这不是逐票取整尾差，"
        f"需核对控制数口径、漏票或运价版本，暂不可确认。",
        TAIL_UNEXPLAINED,
    )


def matched(tail: Decimal, bound: Decimal) -> bool:
    return abs(tail) <= bound


def status_for(has_control: bool, bill_count: int, tail: Decimal, bound: Decimal,
               confirmed: bool, drift: bool = False) -> str:
    if confirmed:
        return STATUS_DRIFT if drift else STATUS_CONFIRMED
    if not has_control:
        return STATUS_NO_CONTROL
    if bill_count == 0:
        return STATUS_NO_BILLS
    return STATUS_READY if matched(tail, bound) else STATUS_REVIEW
