"""M8 月结：控制数、逐票金额不动、尾差归属与重跑幂等。"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from fixtures.expected import AMOUNT_TOTAL

from app import config, store
from app.errors import DomainError
from app.modules.m8_settlement import csv_out, period, service, tail

MONTH = "2026-04"
CONTROL_FILE = "carrier_controls_2026-04.csv"

# 样例脏运单 2026-04 逐承运商合计；3 月只有 WB-1013 一票 19.44。
EXPECTED = {
    "JD": {"count": 4, "billed": "334.33", "control": "334.33", "tail": "0.00"},
    "SF": {"count": 8, "billed": "1033.68", "control": "1033.70", "tail": "0.02"},
    "YT": {"count": 2, "billed": "133.59", "control": "133.57", "tail": "-0.02"},
}


def _controls_bytes(rows: list[str]) -> bytes:
    text = "\n".join(["carrier,control_total", *rows]) + "\n"
    return text.encode("utf-8")


@pytest.fixture()
def clean_month(pipeline):
    """每个用例从无控制数、无月结、账单为原始值开始，避免共享会话库互相污染。"""
    run_id = pipeline["run_id"]
    from app.modules.m5_charges import service as charges
    charges.compute_charges(run_id)
    store.execute("DELETE FROM carrier_controls WHERE month = ?", (MONTH,))
    store.execute("DELETE FROM settlements WHERE month = ?", (MONTH,))
    store.execute("DELETE FROM settlement_lines WHERE month = ?", (MONTH,))
    return run_id


def _import_sample_controls(month: str = MONTH) -> dict:
    raw = (config.SAMPLE_DIR / CONTROL_FILE).read_bytes()
    return service.import_controls(raw, month, CONTROL_FILE)


def _carriers(report: dict) -> dict:
    return {item["carrier"]: item for item in report["carriers"]}


# --- 月份归属 -------------------------------------------------------------

def test_month_uses_display_timezone():
    # 2026-04-01 07:59:59 +08:00 在 UTC 是 3 月 31 日，月结必须归 4 月。
    assert period.month_of("2026-03-31T23:59:59Z") == "2026-04"
    assert period.month_of("2026-03-31T15:59:59Z") == "2026-03"
    assert period.valid("2026-04") is True
    assert period.valid("2026-13") is False
    assert period.valid("2026-4") is False


def test_preview_rejects_bad_month(pipeline):
    with pytest.raises(DomainError) as exc:
        service.preview(pipeline["run_id"], "2026-13")
    assert exc.value.code == "MONTH_INVALID"


# --- 尾差规则 -------------------------------------------------------------

def test_tail_bound_is_per_bill_plus_control_half():
    assert tail.tail_bound(0) == Decimal("0.01")
    assert tail.tail_bound(4) == Decimal("0.05")
    assert tail.tail_bound(8) == Decimal("0.09")
    assert tail.tail_of(Decimal("1033.70"), Decimal("1033.68")) == Decimal("0.02")
    assert tail.direction_of(Decimal("0.02")) == tail.DIRECTION_UP
    assert tail.direction_of(Decimal("-0.02")) == tail.DIRECTION_DOWN
    assert tail.direction_of(Decimal("0")) == tail.DIRECTION_NONE


def test_tail_beyond_bound_is_unexplained():
    reason, code = tail.explain(Decimal("19.47"), 8, tail.tail_bound(8),
                                Decimal("1053.15"), Decimal("1033.68"))
    assert code == tail.TAIL_UNEXPLAINED
    assert "超出取整上界" in reason


# --- 控制数导入 -----------------------------------------------------------

def test_controls_import_and_replace(clean_month):
    got = _import_sample_controls()
    assert got["count"] == 3
    assert sorted(got["carriers"]) == ["JD", "SF", "YT"]
    rows = store.controls(MONTH)
    assert {row["carrier"] for row in rows} == {"JD", "SF", "YT"}
    assert all(row["tax_included"] == 1 for row in rows)


def test_controls_reject_duplicate_carrier(clean_month):
    with pytest.raises(DomainError) as exc:
        service.parse_controls(_controls_bytes(["SF,1.00", "SF,2.00"]), MONTH, "dup.csv")
    assert exc.value.code == "CONTROL_INVALID"
    assert "CONTROL_DUPLICATE" in exc.value.detail


def test_controls_reject_bad_amount(clean_month):
    with pytest.raises(DomainError) as exc:
        service.parse_controls(_controls_bytes(["SF,abc"]), MONTH, "bad.csv")
    assert exc.value.code == "CONTROL_INVALID"


def test_controls_reject_missing_header(clean_month):
    with pytest.raises(DomainError) as exc:
        service.parse_controls(b"carrier,total\nSF,1.00\n", MONTH, "bad.csv")
    assert exc.value.code == "CONTROL_HEADER"


# --- 对账预览 -------------------------------------------------------------

def test_preview_matches_control_and_keeps_line_amounts(clean_month):
    _import_sample_controls()
    report = service.preview(clean_month, MONTH)
    carriers = _carriers(report)
    assert set(carriers) == {"JD", "SF", "YT"}
    for carrier, expected in EXPECTED.items():
        item = carriers[carrier]
        assert item["bill_count"] == expected["count"]
        assert item["billed_total"] == expected["billed"]
        assert item["control_total"] == expected["control"]
        assert item["tail_amount"] == expected["tail"]
        # 月结合计严格等于控制数，差额只落在尾差一行
        assert Decimal(item["settlement_total"]) == Decimal(expected["control"])
        assert Decimal(item["billed_total"]) + Decimal(item["tail_amount"]) == Decimal(
            expected["control"]
        )
        # 逐票金额来自账单原值：总额之和等于逐票合计，尾差不摊进任何一票
        assert sum((Decimal(line["total_amount"]) for line in item["lines"]),
                   Decimal("0.00")) == Decimal(item["billed_total"])
    assert report["ready"] is True
    assert report["totals"]["tail_total"] == "0.00"
    assert report["totals"]["control_total"] == "1501.60"
    assert report["totals"]["billed_total"] == "1501.60"


def test_preview_does_not_change_charge_lines(clean_month):
    before = store.charge_lines(clean_month)
    before_total = store.get_run(clean_month)["amount_total"]
    _import_sample_controls()
    service.preview(clean_month, MONTH)
    after = store.charge_lines(clean_month)
    assert after == before
    assert store.get_run(clean_month)["amount_total"] == before_total == AMOUNT_TOTAL


def test_month_separates_march_waybill(clean_month):
    _import_sample_controls()
    march = service.preview(clean_month, "2026-03")
    assert march["totals"]["billed_total"] == "19.44"
    assert march["carriers"][0]["status"] == tail.STATUS_NO_CONTROL


def test_tail_beyond_bound_blocks_confirmation(clean_month):
    service.import_controls(_controls_bytes(["SF,2000.00"]), MONTH, "huge.csv")
    report = service.preview(clean_month, MONTH)
    sf = _carriers(report)["SF"]
    assert sf["status"] == tail.STATUS_REVIEW
    assert report["ready"] is False
    with pytest.raises(DomainError) as exc:
        service.confirm(clean_month, MONTH, ["SF"])
    assert exc.value.code == "SETTLEMENT_BLOCKED"


# --- 确认与幂等 -----------------------------------------------------------

def test_confirm_freezes_and_rerun_is_idempotent(clean_month):
    _import_sample_controls()
    first = service.confirm(clean_month, MONTH)
    assert {item["carrier"] for item in first["confirmed"]} == {"JD", "SF", "YT"}
    assert len(store.settlement_lines(MONTH, "SF")) == 8

    frozen_before = store.settlements(MONTH)
    again = service.confirm(clean_month, MONTH)
    assert again["confirmed"] == []
    assert len(again["skipped"]) == 3
    assert store.settlements(MONTH) == frozen_before

    # 重跑整条流水线不会改变已确认金额、也不会重复补差
    from app.modules.m4_weights import service as weights
    from app.modules.m5_charges import service as charges
    from app.modules.m1_waybill import service as waybills
    waybills.import_sample()
    weights.compute_weights(clean_month)
    charges.compute_charges(clean_month)
    third = service.confirm(clean_month, MONTH)
    assert third["confirmed"] == []
    assert store.settlements(MONTH) == frozen_before
    assert store.settlement_lines(MONTH, "YT")[0]["total_amount"] in ("112.97", "20.62")


def test_confirmed_statement_matches_control(clean_month):
    _import_sample_controls()
    service.confirm(clean_month, MONTH)
    rows = service.statement(MONTH)
    assert [row["carrier"] for row in rows] == ["JD", "SF", "YT"]
    for row in rows:
        assert Decimal(row["billed_total"]) + Decimal(row["tail_amount"]) == Decimal(
            row["control_total"]
        )
        assert row["status"] == "confirmed"
        assert len(row["digest"]) == 16
    assert Decimal(sum(Decimal(row["settlement_total"]) for row in rows)) == Decimal("1501.60")


def test_statement_and_lines_csv(clean_month):
    _import_sample_controls()
    service.confirm(clean_month, MONTH)
    statement = list(csv.DictReader(io.StringIO(csv_out.statement_csv(MONTH))))
    assert len(statement) == 3
    assert statement[0]["tail_bound"]
    lines = list(csv.DictReader(io.StringIO(csv_out.lines_csv(MONTH))))
    assert len(lines) == 14  # 4 JD + 8 SF + 2 YT
    assert {row["carrier"] for row in lines} == {"JD", "SF", "YT"}


def test_drift_blocks_and_force_reconfirms(clean_month):
    _import_sample_controls()
    service.confirm(clean_month, MONTH)
    old = {row["carrier"]: row["digest"] for row in store.settlements(MONTH)}

    # 票价重算后出现 1 分变化：已确认展示保持不变，但状态标数据已变
    store.execute(
        "UPDATE charge_lines SET total_amount = '20.63' "
        "WHERE run_id = ? AND waybill_no = 'WB-1020'", (clean_month,)
    )
    report = service.preview(clean_month, MONTH)
    jd = _carriers(report)["JD"]
    assert jd["status"] == tail.STATUS_DRIFT
    assert jd["billed_total"] == "334.33"  # 冻结值，不随重算变动
    assert any("WB-1020:20.62 → 20.63" in note for note in jd["drift"])
    assert jd["current"]["billed_total"] == "334.34"

    with pytest.raises(DomainError):
        service.confirm(clean_month, MONTH, ["JD"])

    forced = service.confirm(clean_month, MONTH, ["JD"], force=True)
    assert forced["confirmed"][0]["superseded_digest"] == old["JD"]
    assert forced["confirmed"][0]["billed_total"] == "334.34"
    assert store.settlements(MONTH)[0]["tail_amount"] == "-0.01"


def test_control_change_after_confirm_marks_drift(clean_month):
    _import_sample_controls()
    service.confirm(clean_month, MONTH)
    service.import_controls(_controls_bytes(["JD,334.33", "SF,1033.71", "YT,133.57"]),
                            MONTH, "v2.csv")
    report = service.preview(clean_month, MONTH)
    sf = _carriers(report)["SF"]
    assert sf["status"] == tail.STATUS_DRIFT
    assert any("控制数" in note for note in sf["drift"])


def test_export_requires_all_carriers_confirmed(clean_month):
    _import_sample_controls()
    service.confirm(clean_month, MONTH, ["JD", "YT"])
    with pytest.raises(DomainError) as exc:
        csv_out.statement_csv(MONTH)
    assert exc.value.code == "SETTLEMENT_INCOMPLETE"
    assert "SF" in exc.value.detail
    with pytest.raises(DomainError):
        csv_out.lines_csv(MONTH)


def test_export_rejects_drift_after_confirm(clean_month):
    _import_sample_controls()
    service.confirm(clean_month, MONTH)
    assert csv_out.statement_csv(MONTH)
    service.import_controls(_controls_bytes(["JD,334.33", "SF,1033.71", "YT,133.57"]),
                            MONTH, "v2.csv")
    with pytest.raises(DomainError) as exc:
        csv_out.statement_csv(MONTH)
    assert exc.value.code == "SETTLEMENT_DRIFTED"
    assert "SF" in exc.value.detail


def test_export_without_confirmation_rejected(clean_month):
    _import_sample_controls()
    with pytest.raises(DomainError) as exc:
        csv_out.statement_csv(MONTH)
    assert exc.value.code == "SETTLEMENT_INCOMPLETE"


def test_confirm_unknown_carrier(clean_month):
    _import_sample_controls()
    with pytest.raises(DomainError) as exc:
        service.confirm(clean_month, MONTH, ["XX"])
    assert exc.value.code == "CARRIER_UNKNOWN"


def test_frozen_display_keeps_removed_waybill(clean_month):
    """确认后某票从账单消失：页面仍按冻结快照显示该票与已确认合计。"""
    _import_sample_controls()
    service.confirm(clean_month, MONTH)
    store.execute(
        "DELETE FROM charge_lines WHERE run_id = ? AND waybill_no = 'WB-1020'",
        (clean_month,),
    )
    report = service.preview(clean_month, MONTH)
    jd = _carriers(report)["JD"]
    assert jd["status"] == tail.STATUS_DRIFT
    assert any("不再出现" in note for note in jd["drift"])
    assert jd["billed_total"] == "334.33"          # 已确认值不动
    assert len(jd["lines"]) == 4                   # 冻结的 4 票都在
    assert "WB-1020" in {line["waybill_no"] for line in jd["lines"]}
    assert jd["current"]["bill_count"] == 3
    assert jd["current"]["billed_total"] == "313.71"


def test_confirm_without_control_blocks(clean_month):
    with pytest.raises(DomainError) as exc:
        service.confirm(clean_month, MONTH)
    assert exc.value.code == "SETTLEMENT_BLOCKED"
    assert "缺控制数" in exc.value.detail
