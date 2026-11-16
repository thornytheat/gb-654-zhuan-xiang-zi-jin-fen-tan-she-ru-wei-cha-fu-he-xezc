"""M7 合并映射：纯函数与 SQLite 仓储测试。"""
from __future__ import annotations

import pytest
from decimal import Decimal

from app.modules.m7_mergers import core
from app.modules.m7_mergers.repository import MergeStore, StaleVersion


ENTRIES = [
    {"old_code": "000101", "old_entity": "城北公路段",
     "new_code": "X2026001", "new_entity": "市政交通中心", "weight": "2"},
    {"old_code": "000102", "old_entity": "城南养护站",
     "new_code": "X2026001", "new_entity": "市政交通中心", "weight": "1"},
]


# ---------- core.parse_entries ----------
def test_batch_normalized():
    assert core.batch_text("2026-3") == "2026-03"


@pytest.mark.parametrize("bad", ["", "N/A", "2026/11", "2026-13", "abcd-11"])
def test_batch_invalid(bad):
    with pytest.raises(ValueError):
        core.batch_text(bad)


def test_duplicate_old_code_quarantined():
    good, bad = core.parse_entries(core.SAMPLE["entries"])
    # 样例中 000101 出现两次：第二行进隔离
    assert len(good) == 3
    assert len(bad) == 1
    assert bad[0]["reason"] == "DUPLICATE_CONFLICT"
    assert bad[0]["old_code"] == "000101"


def test_missing_field_quarantined():
    rows = ENTRIES + [{"old_code": "X", "new_code": "Y"}]
    good, bad = core.parse_entries(rows)
    assert len(good) == 2 and len(bad) == 1
    assert bad[0]["reason"] == "EMPTY_VALUE"


def test_negative_weight_quarantined():
    rows = ENTRIES + [
        {"old_code": "Z", "old_entity": "a", "new_code": "Y",
         "new_entity": "b", "weight": "-1"}]
    good, bad = core.parse_entries(rows)
    assert bad[0]["reason"] == "NEGATIVE_WEIGHT"


def test_weight_defaults_to_one():
    row = {"old_code": "Z", "old_entity": "a", "new_code": "Y",
           "new_entity": "b"}
    good, bad = core.parse_entries([row])
    assert good[0]["weight"] == Decimal(1) and not bad


def test_digest_stable_and_order_independent():
    g1, _ = core.parse_entries(ENTRIES)
    g2, _ = core.parse_entries(list(reversed(ENTRIES)))
    assert core.mapping_digest(g1) == core.mapping_digest(g2)


def test_resolve_version_left_closed():
    versions = [
        {"status": "ACTIVE", "effective_batch": "2026-10", "version": 1},
        {"status": "ACTIVE", "effective_batch": "2026-12", "version": 2},
        {"status": "DRAFT", "effective_batch": "2026-09", "version": 3},
    ]
    # 2026-11 落在 10 与 12 之间（左闭右开），命中 10 月版本
    picked = core.resolve_version(versions, "2026-11")
    assert picked["version"] == 1
    assert core.resolve_version(versions, "2026-09") is None
    assert core.resolve_version(versions, "2026-12")["version"] == 2


# ---------- core.bind_budget_rows ----------
@pytest.fixture
def entries():
    good, _ = core.parse_entries(ENTRIES)
    return good


def test_legacy_row_claims_old_entity(entries):
    parsed = [core.parse_budget_row(
        {"project_id": "000101", "amount": "1200.50", "weight": "2",
         "cap": "900", "cap_source": "000101-批复"}, 2)]
    ledger, bad = core.bind_budget_rows(
        parsed, entries, "2026-11", core.current_new_codes(entries))
    row = ledger[0]
    assert row["claimed_entity"] == "城北公路段"          # 旧补传认原主体
    assert row["mapped_new_code"] == "X2026001"          # 归集到新编码
    assert row["is_legacy"] is True
    assert row["cap_source"] == "000101-批复"            # 封顶来源保留
    assert not bad


def test_new_code_row_accepted(entries):
    parsed = [core.parse_budget_row(
        {"project_id": "X2026001", "amount": "100", "weight": "1",
         "cap": "90"}, 2)]
    ledger, bad = core.bind_budget_rows(
        parsed, entries, "2026-11", core.current_new_codes(entries))
    assert ledger[0]["is_legacy"] is False and not bad


def test_unmapped_code_quarantined(entries):
    parsed = [core.parse_budget_row(
        {"project_id": "GHOST", "amount": "100", "weight": "1",
         "cap": "90"}, 2)]
    ledger, bad = core.bind_budget_rows(
        parsed, entries, "2026-11", core.current_new_codes(entries))
    assert not ledger and bad[0]["reason"] == "UNMAPPED_CODE"


def test_duplicate_budget_row_quarantined(entries):
    parsed = [
        core.parse_budget_row(
            {"project_id": "000101", "amount": "100", "weight": "1",
             "cap": "90"}, 2),
        core.parse_budget_row(
            {"project_id": "000101", "amount": "200", "weight": "1",
             "cap": "90"}, 3),
    ]
    ledger, bad = core.bind_budget_rows(
        parsed, entries, "2026-11", core.current_new_codes(entries))
    assert len(ledger) == 1 and bad[0]["reason"] == "DUPLICATE_CONFLICT"


def test_aggregate_weights_and_member_trace(entries):
    parsed = [
        core.parse_budget_row(
            {"project_id": "000101", "amount": "1200.50", "weight": "2",
             "cap": "900"}, 2),
        core.parse_budget_row(
            {"project_id": "000102", "amount": "800.00", "weight": "1",
             "cap": "700", "cap_source": "专项补助"}, 3),
    ]
    ledger, _ = core.bind_budget_rows(
        parsed, entries, "2026-11", core.current_new_codes(entries))
    groups = core.aggregate_weights(ledger)
    assert len(groups) == 1
    g = groups[0]
    assert g["new_code"] == "X2026001"
    assert g["weight"] == Decimal(3)
    assert g["share"] == Decimal(1)
    assert g["amount"] == Decimal("2000.50")
    # 合并后各原项目金额与封顶来源仍可追查
    members = {m["project_id"]: m for m in g["members"]}
    assert members["000101"]["amount"] == "1200.50"
    assert members["000102"]["cap_source"] == "专项补助"


def test_zero_weight_group_rejected(entries):
    parsed = [core.parse_budget_row(
        {"project_id": "000101", "amount": "100", "weight": "0",
         "cap": "90"}, 2)]
    ledger, _ = core.bind_budget_rows(
        parsed, entries, "2026-11", core.current_new_codes(entries))
    with pytest.raises(ValueError):
        core.aggregate_weights(ledger)


# ---------- repository ----------
@pytest.fixture
def store(tmp_path):
    return MergeStore(str(tmp_path / "m7.db"))


def test_publish_two_phase_and_recover(store):
    v = store.begin_publish("2026-11", "d", 2, 1, "n", interrupt=True)
    assert v["status"] == "INTERRUPTED"
    assert store.list_interrupted()[0]["id"] == v["id"]
    recovered = store.recover_interrupted()
    assert recovered["status"] == "ACTIVE"
    assert not store.list_interrupted()


def test_stale_version_rejected_in_transaction(store):
    store.begin_publish("2026-10", "d1", 1, 0, None, False)
    with pytest.raises(StaleVersion):
        store.begin_publish("2026-11", "d2", 1, 0, None, False,
                            expected_version=0)


def test_activate_supersedes_same_batch(store):
    v1 = store.begin_publish("2026-11", "d1", 1, 0, None, False)
    store.activate(v1["id"], None)
    v2 = store.begin_publish("2026-11", "d2", 1, 0, None, False)
    store.activate(v2["id"], None)
    assert store.get_version(v1["id"])["status"] == "SUPERSEDED"
    assert store.get_version(v2["id"])["status"] == "ACTIVE"


def test_post_import_idempotent(store, entries):
    v = store.begin_publish("2026-11", "d", 2, 0, None, False)
    store.insert_entries(v["id"], entries)
    store.activate(v["id"], None)
    parsed = [core.parse_budget_row(
        {"project_id": "000101", "amount": "100", "weight": "1",
         "cap": "90"}, 2)]
    ledger, bad = core.bind_budget_rows(
        parsed, entries, "2026-11", core.current_new_codes(entries))
    first = store.post_import("2026-11", v["id"], "digest-x", ledger, bad,
                              "key-1")
    second = store.post_import("2026-11", v["id"], "digest-x", ledger, bad,
                               "key-1")
    assert first["id"] == second["id"]
    # 台账行只有一份，没有重复入账
    assert len(store.ledger_for(first["id"])) == 1


def test_quarantine_isolated_from_valid_total(store, entries):
    v = store.begin_publish("2026-11", "d", 1, 1, None, False)
    store.insert_quarantine("mapping_publish", v["id"], [
        {"source_line": 5, "old_code": "000101",
         "reason": "DUPLICATE_CONFLICT", "raw": {}}])
    q = store.quarantine_for("mapping_publish", v["id"])
    assert len(q) == 1 and q[0]["reason"] == "DUPLICATE_CONFLICT"
