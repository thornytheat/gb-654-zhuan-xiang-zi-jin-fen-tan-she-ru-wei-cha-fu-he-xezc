"""M7 合并映射：服务层与 HTTP API 测试（含并发与跨批次漂移）。"""
from __future__ import annotations

import concurrent.futures

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.modules.m7_mergers import service
from app.modules.m7_mergers.repository import MergeStore

ENTRIES = [
    {"old_code": "000101", "old_entity": "城北公路段",
     "new_code": "X2026001", "new_entity": "市政交通中心", "weight": "2"},
    {"old_code": "000102", "old_entity": "城南养护站",
     "new_code": "X2026001", "new_entity": "市政交通中心", "weight": "1"},
    {"old_code": "000101", "old_entity": "重复主体",
     "new_code": "X2026009", "new_entity": "其他", "weight": "1"},
]
BUDGET_ROWS = [
    {"project_id": "000101", "amount": "1,200.50", "unit": "元",
     "weight": "2", "cap": "900", "cap_source": "000101-本级批复"},
    {"project_id": "000102", "amount": "800.00", "weight": "1",
     "cap": "700", "cap_source": "000102-专项补助"},
    {"project_id": "GHOST", "amount": "50.00", "weight": "1", "cap": "50"},
]


def _client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "api.db")))
    return TestClient(app), app.state.merge_store


def _register(client, batch="2026-11", entries=ENTRIES, **extra):
    payload = {"effective_batch": batch, "entries": entries, **extra}
    return client.post("/api/mergers/mappings/register", json=payload)


def test_page_contains_merger_workspace(client):
    html = client.get("/budgets").text
    assert 'id="mergers"' in html
    assert "/api/mergers/mappings/register" in html


def test_register_publishes_and_quarantines_duplicate(client):
    r = _register(client)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "PUBLISHED"
    assert body["mapping_version"]["status"] == "ACTIVE"
    assert body["valid_count"] == 2
    assert body["quarantine_count"] == 1          # 同号冲突隔离
    # 冲突不占有效映射
    assert {e["old_code"] for e in body["entries"]} == {"000101", "000102"}


def test_import_requires_mapping(client):
    r = client.post("/api/mergers/budgets/import",
                    json={"batch": "2026-11", "rows": []})
    assert r.status_code == 422
    assert r.json()["error"] == "MAPPING_NOT_FOUND"


def test_import_binds_version_and_keeps_ledger_trace(client):
    _register(client)
    r = client.post("/api/mergers/budgets/import",
                    json={"batch": "2026-11", "rows": BUDGET_ROWS})
    body = r.json()
    assert body["status"] == "POSTED"
    assert body["mapping_version"]["id"] == "map-2026-11-v1"
    # 有效合计不含被隔离的 GHOST 行
    assert body["valid_total"] == "2000.50"
    assert body["valid_count"] == 2
    assert body["quarantine_count"] == 1
    first = body["ledger"][0]
    assert first["claimed_entity"] == "城北公路段"     # 旧主体认领
    assert first["mapped_new_code"] == "X2026001"
    assert first["cap_source"] == "000101-本级批复"   # 封顶来源保留
    assert first["is_legacy"] is True
    # 隔离记录可查且独立
    assert body["quarantine"][0]["reason"] == "UNMAPPED_CODE"


def test_retry_does_not_double_post(client):
    _register(client)
    payload = {"batch": "2026-11", "rows": BUDGET_ROWS}
    first = client.post("/api/mergers/budgets/import", json=payload).json()
    second = client.post("/api/mergers/budgets/import", json=payload).json()
    assert second["replayed"] is True
    assert first["import"]["id"] == second["import"]["id"]
    # 带幂等键重试同样不新增
    keyed = client.post("/api/mergers/budgets/import",
                        json={**payload, "request_key": "rk-1"})
    assert keyed.json()["import"]["id"] == first["import"]["id"]


def test_weights_bind_same_version_and_aggregate(client):
    _register(client)
    imp = client.post("/api/mergers/budgets/import",
                      json={"batch": "2026-11", "rows": BUDGET_ROWS}).json()
    r = client.post("/api/mergers/weights/calculate",
                    json={"batch": "2026-11",
                          "import_id": imp["import"]["id"]})
    body = r.json()
    assert body["mapping_version_id"] == imp["mapping_version"]["id"]
    assert body["share_sum"] == "1"
    group = body["groups"][0]
    assert group["new_code"] == "X2026001"
    assert group["weight"] == "3"
    # 成员行保留合并前金额
    assert {m["project_id"] for m in group["members"]} == {"000101", "000102"}


def test_weight_version_mismatch_rejected(client):
    _register(client, batch="2026-10")
    _register(client, batch="2026-11", entries=ENTRIES[:2])
    imp = client.post("/api/mergers/budgets/import",
                      json={"batch": "2026-11", "rows": BUDGET_ROWS}).json()
    r = client.post("/api/mergers/weights/calculate",
                    json={"mapping_version_id": "map-2026-10-v1",
                          "import_id": imp["import"]["id"]})
    assert r.status_code == 422
    assert r.json()["error"] == "VERSION_MISMATCH"


def test_old_batch_rerun_never_drifts(client):
    # 11 月与 12 月各有一个生效版本
    _register(client, batch="2026-11")
    _register(client, batch="2026-12", entries=[
        {"old_code": "000101", "old_entity": "新主体公司",
         "new_code": "X2030001", "new_entity": "新集团", "weight": "5"}])
    # 重跑 11 月批次：按左闭右开仍解析 11 月版本，主体不漂移
    old = client.post("/api/mergers/budgets/import", json={
        "batch": "2026-11",
        "rows": [{"project_id": "000101", "amount": "10.00",
                  "weight": "2", "cap": "9"}]}).json()
    assert old["mapping_version"]["id"] == "map-2026-11-v1"
    assert old["ledger"][0]["claimed_entity"] == "城北公路段"
    # 显式指定历史版本同样不漂移
    explicit = client.post("/api/mergers/budgets/import", json={
        "batch": "2026-12", "mapping_version_id": "map-2026-11-v1",
        "rows": [{"project_id": "000101", "amount": "10.00",
                  "weight": "2", "cap": "9"}]}).json()
    assert explicit["mapping_version"]["id"] == "map-2026-11-v1"


def test_same_code_different_entity_across_batches(client):
    _register(client, batch="2026-11", entries=ENTRIES[:2])
    _register(client, batch="2027-01", entries=[
        {"old_code": "000101", "old_entity": "桥隧接管主体",
         "new_code": "X2027001", "new_entity": "桥隧集团", "weight": "2"}])
    body = client.post("/api/mergers/budgets/import", json={
        "batch": "2027-01",
        "rows": [{"project_id": "000101", "amount": "3.00",
                  "weight": "2", "cap": "2",
                  "cap_source": "历史批复#88"}]}).json()
    row = body["ledger"][0]
    assert row["claimed_entity"] == "桥隧接管主体"
    assert row["mapped_new_code"] == "X2027001"
    assert row["cap_source"] == "历史批复#88"


def test_interrupted_publish_recoverable(client):
    r = _register(client, simulate_interrupt=True)
    assert r.json()["status"] == "INTERRUPTED"
    # 中断期间没有 ACTIVE 版本，采信被拒
    denied = client.post("/api/mergers/budgets/import",
                         json={"batch": "2026-11", "rows": []})
    assert denied.status_code == 422
    rec = client.post("/api/mergers/mappings/recover").json()
    assert rec["status"] == "RECOVERED"
    assert rec["recovered"]["status"] == "ACTIVE"
    # 恢复后可正常采信
    ok = client.post("/api/mergers/budgets/import", json={
        "batch": "2026-11",
        "rows": [{"project_id": "000101", "amount": "10.00",
                  "weight": "2", "cap": "9"}]})
    assert ok.status_code == 200
    # 再次恢复为空操作
    again = client.post("/api/mergers/mappings/recover").json()
    assert again["status"] == "NOTHING_TO_RECOVER"


def test_stale_expected_version_rejected(client):
    _register(client)
    r = _register(client, expected_version=99)
    assert r.status_code == 422
    assert r.json()["error"] == "STALE_VERSION"


def test_concurrent_publish_only_one_wins(tmp_path):
    settings = Settings(db_path=str(tmp_path / "cc.db"))
    app = create_app(settings)
    with TestClient(app) as seed:
        seed.post("/api/mergers/mappings/register",
                  json={"effective_batch": "2026-10", "entries": ENTRIES[:2]})
        seed.post("/api/mergers/mappings/register",
                  json={"effective_batch": "2026-11", "entries": ENTRIES[:2]})

    def fire(i):
        with TestClient(create_app(settings)) as c:
            r = c.post("/api/mergers/mappings/register", json={
                "effective_batch": f"2027-0{i+1}",
                "entries": [{"old_code": f"C{i}", "old_entity": "e",
                             "new_code": f"M{i}", "new_entity": "E",
                             "weight": "1"}],
                "expected_version": 2})
            return r.json().get("error") or r.json()["status"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(fire, range(2)))
    assert sorted(outcomes) == ["PUBLISHED", "STALE_VERSION"]
    store = MergeStore(settings.db_path)
    assert max(v["version"] for v in store.list_versions()) == 3


def test_idempotent_key_replay_same_version(client):
    payload = {"effective_batch": "2026-11", "entries": ENTRIES[:2],
               "request_key": "pub-1"}
    first = client.post("/api/mergers/mappings/register", json=payload).json()
    second = client.post("/api/mergers/mappings/register", json=payload).json()
    assert second["replayed"] is True
    assert first["mapping_version"]["id"] == second["mapping_version"]["id"]


def test_trace_and_quarantine_endpoints(client):
    _register(client)
    client.post("/api/mergers/budgets/import",
                json={"batch": "2026-11", "rows": BUDGET_ROWS})
    trace = client.get("/api/mergers/trace").json()
    assert len(trace["versions"]) == 1
    assert len(trace["imports"]) == 1
    assert len(trace["mapping_quarantine"]) == 1
    detail = trace["imports"][0]
    assert detail["ledger"][0]["cap_source"] == "000101-本级批复"
    records = client.get("/api/mergers/quarantine").json()["records"]
    # 映射登记冲突 + 预算未映射，两类隔离独立留存
    reasons = {r["reason"] for r in records}
    assert {"DUPLICATE_CONFLICT", "UNMAPPED_CODE"} <= reasons


def test_service_helpers_with_store(tmp_path):
    store = MergeStore(str(tmp_path / "s.db"))
    out = service.register_mapping(store, {
        "effective_batch": "2026-11", "entries": ENTRIES[:2]})
    assert out["status"] == "PUBLISHED"
    listing = service.list_mappings(store)
    assert listing["versions"][0]["status"] == "ACTIVE"
    weights = service.calculate_weights(store, {"batch": "2026-11"})
    assert weights["groups"][0]["weight"] == "3"
