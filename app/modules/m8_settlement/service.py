"""M8 月结：逐承运商把控制数、逐票已核金额与尾差拼成一份可解释的月结结果。

三层数据严格分开：

1. 控制数（carrier_controls）：承运商给的含税月结总额，唯一的外部口径。
2. 逐票已核金额（charge_lines）：确认过的账单明细，月结前后都不改动。
3. 尾差（settlements.tail_amount）：控制数减逐票合计的差，单独一行归属月结层，
   按票数推导上界，只解释成因，不摊回任何票。

确认（confirm）把当时的逐票金额与逐票摘要冻结成快照。之后同一月份重跑
只做比对：票面一致就原样返回已确认结果（幂等，不重复补差、不改金额）；
票面变了就标「已确认（数据已变）」，要求人工解锁重确认，不静默覆盖。
"""

from __future__ import annotations

import hashlib
import csv
import io
from decimal import Decimal, InvalidOperation

from app import config, manifest, store
from app.errors import DomainError
from app.modules.m5_charges.money import q2
from app.modules.m8_settlement import period, tail


# --- 控制数导入 -----------------------------------------------------------

CONTROL_HEADER = ("carrier", "control_total")


def _dec(text: str) -> Decimal | None:
    raw = (text or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_controls(raw: bytes, month: str, name: str = "") -> dict:
    """解析承运商控制数 CSV：两列 carrier,control_total；同承运商重复即拒绝。"""
    if not period.valid(month):
        raise DomainError("MONTH_INVALID", f"月份格式应为 YYYY-MM：{month}", 422)
    text = None
    for encoding in config.ENCODINGS:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise DomainError("ENCODING_FAILED", "控制数文件无法解码", 422)
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise DomainError("CONTROL_EMPTY", "控制数文件为空", 422)

    header = [(cell or "").strip().lower() for cell in rows[0]]
    missing = [c for c in CONTROL_HEADER if c not in header]
    if missing:
        raise DomainError(
            "CONTROL_HEADER", "控制数表头缺列：" + ",".join(missing), 422
        )
    index = {name_: header.index(name_) for name_ in CONTROL_HEADER}

    items: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for offset, cells in enumerate(rows[1:], start=2):
        if not any((cell or "").strip() for cell in cells):
            continue
        carrier = (cells[index["carrier"]] if len(cells) > index["carrier"] else "").strip().upper()
        amount = _dec(cells[index["control_total"]] if len(cells) > index["control_total"] else "")
        if not carrier:
            problems.append(f"{offset}:MISSING_CARRIER")
            continue
        if carrier in seen:
            problems.append(f"{offset}:CONTROL_DUPLICATE")
            continue
        if amount is None or amount < 0:
            problems.append(f"{offset}:CONTROL_AMOUNT")
            continue
        seen.add(carrier)
        items.append({
            "month": month,
            "carrier": carrier,
            "control_total": str(q2(amount)),
            "currency": config.SETTLEMENT_CURRENCY,
            "tax_included": 1,
            "source_name": name,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "imported_at": manifest.utc_now_iso(),
        })
    if problems:
        raise DomainError("CONTROL_INVALID", ";".join(problems[:8]), 422)
    if not items:
        raise DomainError("CONTROL_EMPTY", "控制数文件没有可导入的行", 422)
    return {"month": month, "count": len(items), "items": items,
            "input_sha256": hashlib.sha256(raw).hexdigest()}


def import_controls(raw: bytes, month: str, name: str = "") -> dict:
    parsed = parse_controls(raw, month, name)
    store.replace_controls(month, parsed["items"])
    return {"month": month, "count": parsed["count"],
            "input_sha256": parsed["input_sha256"],
            "carriers": [item["carrier"] for item in parsed["items"]]}


# --- 逐票快照 -------------------------------------------------------------

def _billed_lines(run_id: str, month: str) -> list[dict]:
    """当前运行里落在该月的计费行，带承运商与逐票摘要。"""
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    lines: list[dict] = []
    for row in store.charge_lines(run_id):
        waybill = waybills.get(row["waybill_no"])
        if waybill is None:
            continue
        if period.month_of(waybill["pickup_utc"]) != month:
            continue
        digest = hashlib.sha256(
            manifest.dumps([row["waybill_no"], row["total_amount"],
                            row["base_amount"], row["step_amount"], row["remote_amount"],
                            row["fuel_amount"], row["insurance_amount"],
                            row["version_id"]]).encode("utf-8")
        ).hexdigest()[:16]
        lines.append({
            "waybill_no": row["waybill_no"],
            "carrier": waybill["carrier"],
            "service": waybill["service"],
            "zone": waybill["zone"],
            "pickup_utc": waybill["pickup_utc"],
            "pickup_local": period.local_stamp(waybill["pickup_utc"]),
            "version_id": row["version_id"],
            "total_amount": q2(Decimal(row["total_amount"])),
            "charge_digest": digest,
        })
    lines.sort(key=lambda item: (item["carrier"], item["waybill_no"]))
    return lines


def _confirmed(run_id: str, month: str) -> dict[str, dict]:
    got = {}
    for row in store.settlements(month):
        if row.get("status") != "confirmed":
            continue
        got[row["carrier"]] = {
            "header": row,
            "lines": {line["waybill_no"]: line for line in
                      store.settlement_lines(month, row["carrier"])},
        }
    return got


def _lines_drift(confirmed_lines: dict[str, dict], current: list[dict]) -> list[str]:
    """比对冻结快照与当前逐票金额：票号集合或金额任一变化即视为变动。"""
    now = {line["waybill_no"]: line for line in current}
    problems: list[str] = []
    for waybill_no, frozen in confirmed_lines.items():
        if waybill_no not in now:
            problems.append(f"{waybill_no}:不再出现在本月账单")
        elif q2(Decimal(now[waybill_no]["total_amount"])) != q2(Decimal(frozen["total_amount"])):
            problems.append(
                f"{waybill_no}:{frozen['total_amount']} → {now[waybill_no]['total_amount']}"
            )
    for waybill_no in now:
        if waybill_no not in confirmed_lines:
            problems.append(f"{waybill_no}:确认后才出现")
    return problems


# --- 预览 / 确认 ----------------------------------------------------------

def months(run_id: str) -> list[str]:
    """可选月份：当前运行的提货月 ∪ 已导入控制数的月 ∪ 已确认月结的月。"""
    found: set[str] = set()
    if run_id:
        waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
        for row in store.charge_lines(run_id):
            waybill = waybills.get(row["waybill_no"])
            if waybill is None:
                continue
            month = period.month_of(waybill["pickup_utc"])
            if month:
                found.add(month)
    found |= {row["month"] for row in store.controls_all()}
    found |= {row["month"] for row in store.settlements_all()}
    return sorted(found, reverse=True)


def preview(run_id: str, month: str) -> dict:
    if not period.valid(month):
        raise DomainError("MONTH_INVALID", f"月份格式应为 YYYY-MM：{month}", 422)
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    current = _billed_lines(run_id, month)
    controls = {row["carrier"]: row for row in store.controls(month)}
    frozen = _confirmed(run_id, month)

    carriers = sorted({line["carrier"] for line in current} | set(controls) | set(frozen))
    items: list[dict] = []
    for carrier in carriers:
        lines = [line for line in current if line["carrier"] == carrier]
        control = controls.get(carrier)
        billed = q2(sum((line["total_amount"] for line in lines), Decimal("0.00")))
        count = len(lines)
        bound = tail.tail_bound(count)
        control_total = q2(Decimal(control["control_total"])) if control else None
        item = {
            "carrier": carrier,
            "bill_count": count,
            "billed_total": str(billed),
            "control_total": str(control_total) if control_total is not None else "",
            "currency": (control or {}).get("currency", config.SETTLEMENT_CURRENCY),
            "tax_included": bool((control or {}).get("tax_included", 1)),
            "control_source": (control or {}).get("source_name", ""),
            "control_sha256": (control or {}).get("source_sha256", ""),
            "tail_bound": str(bound),
            "lines": lines,
        }
        confirmed = frozen.get(carrier)
        if confirmed:
            item.update(_frozen_view(confirmed, lines, control_total))
            items.append(item)
            continue

        if control_total is None:
            item.update({
                "tail_amount": "", "tail_direction": tail.DIRECTION_NONE,
                "tail_reason": "尚未导入该承运商控制数，无法确定尾差。",
                "settlement_total": "", "confirmed_at": "", "digest": "",
                "frozen": False, "drift": [], "matched": False,
                "status": tail.STATUS_NO_CONTROL, "confirmed_lines": [],
            })
            items.append(item)
            continue

        diff = tail.tail_of(control_total, billed)
        reason, _code = tail.explain(diff, count, bound, control_total, billed)
        item.update({
            "tail_amount": str(diff),
            "tail_direction": tail.direction_of(diff),
            "tail_reason": reason,
            "settlement_total": str(q2(billed + diff)),
            "confirmed_at": "", "digest": "",
            "frozen": False, "drift": [], "matched": tail.matched(diff, bound),
            "status": tail.status_for(True, count, diff, bound, False),
            "confirmed_lines": [],
        })
        items.append(item)

    return {
        "run_id": run_id,
        "month": month,
        "carriers": items,
        "ready": bool(items) and all(
            item["status"] in (tail.STATUS_READY, tail.STATUS_CONFIRMED)
            for item in items
        ),
        "confirmed": carried_count(items),
        "totals": _totals(items),
    }


def _frozen_view(confirmed: dict, current_lines: list[dict],
                 control_total: Decimal | None) -> dict:
    """已确认承运商的展示：金额一律取冻结快照，只把当前票面用于漂移比对。

    这样重跑或票价重算都不会让客户看到已确认金额变动；变动只体现在 drift
    清单与「已确认（数据已变）」状态上，等人工复核。
    """
    header = confirmed["header"]
    drift = _lines_drift(confirmed["lines"], current_lines)
    if control_total is None:
        drift.append(f"控制数：{header['control_total']} → 已不在本月控制数文件里")
    elif q2(Decimal(header["control_total"])) != control_total:
        drift.append(f"控制数：{header['control_total']} → {control_total}")
    frozen_lines = [
        {"waybill_no": waybill_no, "total_amount": line["total_amount"]}
        for waybill_no, line in sorted(confirmed["lines"].items())
    ]
    return {
        "bill_count": int(header["bill_count"]),
        "billed_total": header["billed_total"],
        "control_total": header["control_total"],
        "tail_bound": header["tail_bound"],
        "tail_amount": header["tail_amount"],
        "tail_direction": header["tail_direction"],
        "tail_reason": header["tail_reason"],
        "settlement_total": header["settlement_total"],
        "confirmed_at": header["confirmed_at"],
        "digest": header["digest"],
        "frozen": True,
        "drift": drift,
        "status": tail.STATUS_DRIFT if drift else tail.STATUS_CONFIRMED,
        "matched": not drift,
        "confirmed_lines": frozen_lines,
        "lines": _display_lines(current_lines, confirmed["lines"]),
        # 当前票面的实时值，供 force 重确认使用
        "current": _live_totals(current_lines, control_total),
        "current_lines": current_lines,
    }


def _display_lines(current_lines: list[dict], frozen: dict[str, dict]) -> list[dict]:
    """展示行：以已确认快照为准，当前重算值只用于比对。

    已确认的票始终按冻结金额出现；即便重算后该票消失，也从快照补回一行，
    避免客户看到已确认票被悄悄删掉。确认后才出现的票追加在末尾。
    """
    current = {line["waybill_no"]: line for line in current_lines}
    shown: list[dict] = []
    for waybill_no, frozen_line in sorted(frozen.items()):
        line = current.get(waybill_no) or {
            "waybill_no": waybill_no,
            "pickup_local": "", "pickup_utc": "", "version_id": "",
        }
        shown.append({**line, "total_amount": frozen_line["total_amount"]})
    for line in current_lines:
        if line["waybill_no"] not in frozen:
            shown.append(line)
    return shown


def _live_totals(lines: list[dict], control_total: Decimal | None) -> dict:
    billed = q2(sum((line["total_amount"] for line in lines), Decimal("0.00")))
    count = len(lines)
    bound = tail.tail_bound(count)
    data = {
        "bill_count": count,
        "billed_total": str(billed),
        "tail_bound": str(bound),
        "control_total": str(control_total) if control_total is not None else "",
        "tail_amount": "",
        "tail_direction": tail.DIRECTION_NONE,
        "tail_reason": "",
        "settlement_total": "",
        "matched": False,
    }
    if control_total is not None:
        diff = tail.tail_of(control_total, billed)
        reason, _code = tail.explain(diff, count, bound, control_total, billed)
        data.update({
            "tail_amount": str(diff),
            "tail_direction": tail.direction_of(diff),
            "tail_reason": reason,
            "settlement_total": str(q2(billed + diff)),
            "matched": tail.matched(diff, bound),
        })
    return data


def carried_count(items: list[dict]) -> int:
    return sum(1 for item in items if item["status"] == tail.STATUS_CONFIRMED)


def _totals(items: list[dict]) -> dict:
    billed = q2(sum((Decimal(item["billed_total"]) for item in items), Decimal("0.00")))
    control = q2(sum(
        (Decimal(item["control_total"]) for item in items if item["control_total"]),
        Decimal("0.00"),
    ))
    tail_total = q2(sum(
        (Decimal(item["tail_amount"]) for item in items if item["tail_amount"]),
        Decimal("0.00"),
    ))
    return {
        "billed_total": str(billed),
        "control_total": str(control),
        "tail_total": str(tail_total),
    }


def _digest(basis: dict, lines: list[dict]) -> str:
    payload = manifest.dumps([
        basis["carrier"], basis["bill_count"], basis["billed_total"],
        basis["control_total"], basis["tail_amount"], basis["tail_bound"],
        [[line["waybill_no"], str(line["total_amount"]), line["charge_digest"]]
         for line in lines],
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def confirm(run_id: str, month: str, carriers: list[str] | None = None,
            force: bool = False) -> dict:
    """冻结当前月结结果。已确认且票面未变的承运商直接跳过（幂等）。

    已确认但票面/控制数有变的承运商默认拒绝；只有显式 force 才允许用当前票面
    重确认，且返回值里保留被覆盖的旧摘要。任何情况下都不改动逐票账单本身，
    尾差只记在月结层。
    """
    report = preview(run_id, month)
    targets = sorted(set(carriers or [item["carrier"] for item in report["carriers"]]))
    known = {item["carrier"]: item for item in report["carriers"]}
    missing = [carrier for carrier in targets if carrier not in known]
    if missing:
        raise DomainError("CARRIER_UNKNOWN", "本月没有该承运商：" + ",".join(missing), 404)

    now = manifest.utc_now_iso()
    confirmed: list[dict] = []
    skipped: list[dict] = []
    blocked: list[dict] = []
    for carrier in targets:
        item = known[carrier]
        if item["status"] == tail.STATUS_CONFIRMED:
            skipped.append({"carrier": carrier, "reason": "已确认且票面未变",
                            "digest": item["digest"]})
            continue
        if item["status"] == tail.STATUS_DRIFT and not force:
            blocked.append({"carrier": carrier, "reason": "已确认后数据变动，需 force 重确认",
                            "drift": item["drift"]})
            continue

        superseded = None
        if item["status"] == tail.STATUS_DRIFT:
            # force 重确认：用当前票面的实时值重建，旧摘要留档。
            superseded = item["digest"]
            basis = {
                "carrier": carrier,
                "bill_count": item["current"]["bill_count"],
                "billed_total": item["current"]["billed_total"],
                "control_total": item["current"]["control_total"],
                "tail_amount": item["current"]["tail_amount"],
                "tail_direction": item["current"]["tail_direction"],
                "tail_bound": item["current"]["tail_bound"],
                "tail_reason": item["current"]["tail_reason"],
                "settlement_total": item["current"]["settlement_total"],
                "matched": item["current"]["matched"],
            }
            lines = item["current_lines"]
        else:
            basis = item
            lines = item["lines"]

        if not basis["control_total"]:
            blocked.append({"carrier": carrier, "reason": "缺控制数",
                            "status": tail.STATUS_NO_CONTROL})
            continue
        if basis["bill_count"] == 0:
            blocked.append({"carrier": carrier, "reason": "本月无计费票",
                            "status": tail.STATUS_NO_BILLS})
            continue
        if not basis["matched"]:
            blocked.append({"carrier": carrier, "reason": basis["tail_reason"],
                            "status": tail.STATUS_REVIEW})
            continue

        digest = _digest(basis, lines)
        header = {
            "month": month,
            "carrier": carrier,
            "control_total": basis["control_total"],
            "billed_total": basis["billed_total"],
            "bill_count": basis["bill_count"],
            "tail_amount": basis["tail_amount"],
            "tail_direction": basis["tail_direction"],
            "tail_bound": basis["tail_bound"],
            "tail_reason": basis["tail_reason"],
            "settlement_total": basis["settlement_total"],
            "run_id": run_id,
            "digest": digest,
            "status": "confirmed",
            "confirmed_at": now,
            "updated_at": now,
        }
        frozen_lines = [
            {
                "waybill_no": line["waybill_no"],
                "total_amount": str(line["total_amount"]),
                "charge_digest": line["charge_digest"],
                "confirmed_at": now,
            }
            for line in lines
        ]
        store.save_settlement(month, carrier, header, frozen_lines)
        entry = {
            "carrier": carrier,
            "bill_count": basis["bill_count"],
            "billed_total": basis["billed_total"],
            "control_total": basis["control_total"],
            "tail_amount": basis["tail_amount"],
            "settlement_total": basis["settlement_total"],
            "digest": digest,
        }
        if superseded:
            entry["superseded_digest"] = superseded
        confirmed.append(entry)

    if blocked and not confirmed and not skipped:
        raise DomainError(
            "SETTLEMENT_BLOCKED",
            "；".join(f"{b['carrier']}：{b['reason']}" for b in blocked),
            409,
        )
    return {
        "month": month,
        "run_id": run_id,
        "confirmed": confirmed,
        "skipped": skipped,
        "blocked": blocked,
        "report": preview(run_id, month),
    }


# --- 冻结结果的取用 -------------------------------------------------------

def frozen_lines(month: str, carrier: str) -> list[dict]:
    """导出用：已确认的逐票金额优先取冻结快照，保证确认后金额不再变化。"""
    return store.settlement_lines(month, carrier)


def statement(month: str) -> list[dict]:
    """已确认月结表：每承运商一行控制数、账单合计、尾差与月结合计。"""
    out = []
    for row in store.settlements(month):
        out.append({
            "month": row["month"],
            "carrier": row["carrier"],
            "bill_count": int(row["bill_count"]),
            "billed_total": row["billed_total"],
            "control_total": row["control_total"],
            "tail_amount": row["tail_amount"],
            "tail_direction": row["tail_direction"],
            "tail_bound": row["tail_bound"],
            "tail_reason": row["tail_reason"],
            "settlement_total": row["settlement_total"],
            "status": row["status"],
            "digest": row["digest"],
            "run_id": row["run_id"],
            "confirmed_at": row["confirmed_at"],
        })
    return out
