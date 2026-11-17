"""月结导出：承运商月结单（一行一承运商）、已确认逐票明细。

导出有闸门：当月控制数覆盖的每一家都必须已确认，且逐票合计 + 尾差必须等于控制数；
任何一家没对平就拒绝导出，避免拿到一份看起来整齐、实际缺承运商的月结单。
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from app import store
from app.errors import DomainError
from app.modules.m5_charges.money import q2
from app.modules.m8_settlement import service, tail

STATEMENT_HEADER = [
    "month", "carrier", "bill_count", "billed_total", "control_total",
    "tail_direction", "tail_amount", "tail_bound", "settlement_total",
    "tail_reason", "status", "digest", "confirmed_at",
]

LINE_HEADER = [
    "month", "carrier", "waybill_no", "total_amount", "confirmed_at",
]


def check(month: str) -> list[dict]:
    """导出前闸门：控制数各承运商已确认、账对平、且确认后票面未再变动。"""
    controls = {row["carrier"]: row for row in store.controls(month)}
    rows = service.statement(month)
    settled = {row["carrier"]: row for row in rows}
    missing = sorted(set(controls) - set(settled))
    if missing:
        raise DomainError(
            "SETTLEMENT_INCOMPLETE",
            "当月还有承运商未确认月结，不能导出：" + ",".join(missing),
            409,
        )
    if not rows:
        raise DomainError("SETTLEMENT_EMPTY", f"{month} 没有已确认的月结", 409)

    # 已确认后控制数或票面若被改动，导出的是过期结果，必须先复核。
    run_ids = {row["run_id"] for row in rows if row["run_id"]}
    if len(run_ids) == 1:
        live = {item["carrier"]: item
                for item in service.preview(run_ids.pop(), month)["carriers"]}
        drifted = sorted(
            carrier for carrier in settled
            if live.get(carrier, {}).get("status") == tail.STATUS_DRIFT
        )
        if drifted:
            raise DomainError(
                "SETTLEMENT_DRIFTED",
                "以下承运商确认后数据已变动，需复核后重确认：" + ",".join(drifted),
                409,
            )

    for row in rows:
        if q2(Decimal(row["billed_total"]) + Decimal(row["tail_amount"])) != q2(
            Decimal(row["control_total"])
        ):
            raise DomainError(
                "SETTLEMENT_UNBALANCED",
                f"{row['carrier']}：逐票合计 + 尾差 ≠ 控制数",
                409,
            )
    carried = q2(sum((Decimal(row["settlement_total"]) for row in rows), Decimal("0.00")))
    expected = q2(sum(
        (Decimal(row["control_total"]) for row in rows), Decimal("0.00")
    ))
    if carried != expected:
        raise DomainError("SETTLEMENT_UNBALANCED", "月结合计与控制数合计不等", 409)
    return rows


def statement_csv(month: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(STATEMENT_HEADER)
    for row in check(month):
        writer.writerow([row[key] for key in STATEMENT_HEADER])
    return buffer.getvalue()


def lines_csv(month: str) -> str:
    check(month)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(LINE_HEADER)
    for header in store.settlements(month):
        for line in store.settlement_lines(month, header["carrier"]):
            writer.writerow([
                month, header["carrier"], line["waybill_no"],
                line["total_amount"], line["confirmed_at"],
            ])
    return buffer.getvalue()
