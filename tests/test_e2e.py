"""端到端：样例脏运单 → 采信 → 计费重 → 账单 → 对账 → 导出，以及页面可用。"""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fixtures.expected import AMOUNT_TOTAL, ROWS_IN, ROWS_OK, ROWS_QUARANTINED

from app.main import app
from app.modules.m6_reconcile import service as reconcile


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_sample_flow_end_to_end(pipeline):
    report = reconcile.reconcile(pipeline["run_id"])
    assert report["rows_in"] == ROWS_IN
    assert report["rows_ok"] == ROWS_OK
    assert report["amount_total"] == AMOUNT_TOTAL
    assert report["verdict"]["balanced"] is True


def test_web_pages_return_200(client):
    pages = ["/", "/waybills", "/weights", "/charges", "/reconcile",
             "/settlement", "/export"]
    for page in pages:
        response = client.get(page)
        assert response.status_code == 200, page
        assert "freightrate" in response.text
    assert len(pages) == 7


def test_api_health(client):
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert payload["display_tz"] == "Asia/Shanghai"
    assert payload["rate_set_version"] == 3


def test_export_endpoints_are_not_empty(client):
    invoice = client.get("/api/export/invoice.csv")
    assert invoice.status_code == 200
    assert invoice.text.strip().splitlines()[0].startswith("waybill_no")

    evidence = client.get("/api/export/evidence.csv")
    assert evidence.status_code == 200
    assert "line_no" in evidence.text.splitlines()[0]

    manifest = client.get("/api/export/manifest.json")
    assert manifest.status_code == 200
    doc = json.loads(manifest.text)
    assert doc["rows"]["input"] == ROWS_IN
    assert len(doc["input_sha256"]) == 64


def test_reconcile_endpoint_balanced(client):
    report = client.get("/api/reconcile").json()["report"]
    assert report["verdict"]["balanced"] is True
    assert report["total"] == AMOUNT_TOTAL


def test_quarantine_endpoints(client):
    payload = client.get("/api/quarantine").json()
    assert payload["metrics"]["quarantined_lines"] == ROWS_QUARANTINED
    body = client.get("/api/quarantine.csv")
    assert body.status_code == 200
    assert body.text.strip().splitlines()[0].startswith("line_no")


def test_settlement_end_to_end(client):
    """控制数导入 → 预览对上 → 确认 → 导出；重复确认不重复补差。"""
    month = "2026-04"
    imported = client.post(
        "/api/settlement/controls",
        data={"month": month, "sample": "carrier_controls_2026-04.csv"},
    )
    assert imported.status_code == 200
    assert imported.json()["result"]["count"] == 3

    report = client.get(f"/api/settlement?month={month}").json()["report"]
    carriers = {item["carrier"]: item for item in report["carriers"]}
    assert carriers["SF"]["tail_amount"] == "0.02"
    assert carriers["YT"]["tail_amount"] == "-0.02"
    assert report["ready"] is True
    for item in report["carriers"]:
        assert Decimal(item["billed_total"]) + Decimal(item["tail_amount"]) == Decimal(
            item["control_total"]
        )

    first = client.post("/api/settlement/confirm", data={"month": month}).json()["result"]
    assert len(first["confirmed"]) == 3

    again = client.post("/api/settlement/confirm", data={"month": month}).json()["result"]
    assert again["confirmed"] == []
    assert len(again["skipped"]) == 3

    statement = client.get(f"/api/settlement/statement.csv?month={month}")
    assert statement.status_code == 200
    rows = list(csv.DictReader(io.StringIO(statement.text)))
    assert [row["carrier"] for row in rows] == ["JD", "SF", "YT"]
    lines = client.get(f"/api/settlement/lines.csv?month={month}")
    assert len(lines.text.strip().splitlines()) == 15  # 表头 + 14 票
