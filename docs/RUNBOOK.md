# 运行手册 freightrate

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18209/api/health
```

宿主机端口 18209 映射容器 8000。SQLite 落在命名卷 `freightrate-data` 的 `/app/data/freightrate.db`。

## 启动后自动完成的事

1. 建表
2. 装载 `sample_data/rate_cards.csv`（运价集 v3，38 行）
3. 采信 `sample_data/waybills_dirty.csv`
4. 计算计费重与账单

页面首次打开即可看到 27 条运单、15 票计费、12 行隔离、应收 1521.04 元。

## 手工重跑

```bash
curl -s -X POST http://127.0.0.1:18209/api/waybills/import -F sample=waybills_dirty.csv
curl -s -X POST http://127.0.0.1:18209/api/weights
curl -s -X POST http://127.0.0.1:18209/api/charges
curl -s http://127.0.0.1:18209/api/reconcile
```

同一份输入重复导入命中同一个 run_id，行数与合计不变。

## 运价文件校验

```bash
curl -s -X POST http://127.0.0.1:18209/api/rates/audit -F sample=rate_cards_overlap.csv
```

返回重叠清单；`/api/rates/import` 在校验不通过时返回 409，不会替换现有运价。

## 承运商月结

```bash
# 1. 导入当月控制数（两列 carrier,control_total；同月整月替换）
curl -s -X POST http://127.0.0.1:18209/api/settlement/controls \
  -F month=2026-04 -F sample=carrier_controls_2026-04.csv

# 2. 看逐承运商控制数、逐票合计、尾差与状态
curl -s "http://127.0.0.1:18209/api/settlement?month=2026-04"

# 3. 确认（可只传某几家：-F carriers=SF,JD）
curl -s -X POST http://127.0.0.1:18209/api/settlement/confirm -F month=2026-04

# 4. 取月结单与已确认逐票明细
curl -s -o settlement-2026-04.csv "http://127.0.0.1:18209/api/settlement/statement.csv?month=2026-04"
curl -s -o settlement-lines-2026-04.csv "http://127.0.0.1:18209/api/settlement/lines.csv?month=2026-04"
```

样例控制数下：JD 尾差 0.00、SF +0.02、YT −0.02，三家都在上界内，月结合计与控制数相等。
重复执行确认只会返回 `skipped`，不会重复补差，也不会改动已确认金额。

| 月结现象 | 原因 | 处理 |
|---|---|---|
| 状态「需复核」 | 尾差超过票数 × 0.01 + 0.005 元上界 | 核对控制数口径、漏票或运价版本，不要改逐票金额 |
| 状态「已确认（数据已变）」 | 确认后账单或控制数被重算/替换 | 复核 drift 清单；确需采纳新票面时用 `force=1` 重确认 |
| 确认返回 409 SETTLEMENT_BLOCKED | 无一家满足确认条件 | 按 detail 里的承运商原因逐条处理 |
| 状态「缺控制数」 | 该承运商当月没有导入控制数 | 导入控制数后再看月结 |

## 重建数据库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 500，提示 RATE_MISSING | 采信时运价表还没装载 | 先打开运价手册页确认 38 行版本窗口 |
| /api/charges 返回 409 NO_WEIGHTS | 还没算计费重 | 先调 /api/weights |
| 导入后 rows_ok 为 0 | 全部行都被隔离 | 到运单页看原因码分布 |
| 运价装载 409 RATE_STALE | 提交的运价集版本低于现有 | 提高 CSV 里的 rate_set_version |
