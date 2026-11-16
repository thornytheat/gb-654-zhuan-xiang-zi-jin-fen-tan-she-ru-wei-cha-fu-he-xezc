"""合并映射域服务层：发布、恢复、预算采信、权重测算与追查编排。"""
from __future__ import annotations

from decimal import Decimal

from app.errors import DomainError
from app.manifest import canonical_digest
from . import core
from .repository import MergeStore, StaleVersion, WriteConflict


def _conflict(exc: WriteConflict) -> DomainError:
    return DomainError("WRITE_CONFLICT", "并发写入冲突，请刷新版本后重试")


# ---------- 映射登记 + 发布（两阶段、可恢复） ----------
def register_mapping(store: MergeStore, payload: dict) -> dict:
    batch = core.batch_text(payload.get("effective_batch"))
    entries = payload.get("entries") or []
    good, bad = core.parse_entries(entries)
    digest = core.mapping_digest(good)
    request_key = payload.get("request_key")
    cached = store.cached_response(request_key)
    if cached:
        version_id = cached["response_ref"]
        return {"status": "IDEMPOTENT_REPLAY",
                "mapping_version": store.get_version(version_id),
                "valid_count": len(good), "quarantine_count": len(bad),
                "replayed": True}

    expected_version = payload.get("expected_version")
    expected = int(expected_version) if expected_version is not None else None

    # 同内容且同批次已有 ACTIVE：幂等返回原版本，不产生新版本。
    # 但显式携带过期乐观锁版本号时优先拒绝，避免过期请求被幂等放行。
    if expected is None:
        for version in store.list_versions():
            if (version["digest"] == digest
                    and version["effective_batch"] == batch
                    and version["status"] == "ACTIVE"):
                return {"status": "IDEMPOTENT_REPLAY",
                        "mapping_version": version,
                        "valid_count": len(good), "quarantine_count": len(bad),
                        "replayed": True}

    interrupt = bool(payload.get("simulate_interrupt"))
    try:
        # 乐观锁复检在立即事务内完成，并发发布只放行一个
        version = store.begin_publish(batch, digest, len(good), len(bad),
                                      payload.get("note"), interrupt, expected)
        if good:
            store.insert_entries(version["id"], good)
        if bad:
            store.insert_quarantine("mapping_publish", version["id"], bad)
        if interrupt:
            # 切换中断：版本落 INTERRUPTED，等待恢复，不产生 ACTIVE
            return {"status": "INTERRUPTED", "mapping_version": version,
                    "valid_count": len(good), "quarantine_count": len(bad),
                    "replayed": False}
        version = store.activate(version["id"], request_key)
    except StaleVersion as exc:
        raise DomainError("STALE_VERSION", str(exc))
    except WriteConflict as exc:
        raise _conflict(exc)
    return {"status": "PUBLISHED", "mapping_version": version,
            "valid_count": len(good), "quarantine_count": len(bad),
            "entries": _entry_view(store, version["id"]),
            "quarantine": bad, "replayed": False}


def recover(store: MergeStore) -> dict:
    interrupted = store.list_interrupted()
    if not interrupted:
        return {"status": "NOTHING_TO_RECOVER", "recovered": None,
                "interrupted": []}
    try:
        version = store.recover_interrupted()
    except WriteConflict as exc:
        raise _conflict(exc)
    return {"status": "RECOVERED", "recovered": version,
            "interrupted": interrupted,
            "quarantine": store.quarantine_for(
                "mapping_publish", version["id"])}


def list_mappings(store: MergeStore) -> dict:
    versions = store.list_versions()
    return {"status": "READY", "versions": versions,
            "interrupted": store.list_interrupted()}


# ---------- 预算采信：导入与权重绑定同一映射版本 ----------
def import_budget(store: MergeStore, payload: dict) -> dict:
    batch = core.batch_text(payload.get("batch"))
    request_key = payload.get("request_key")
    cached = store.cached_response(request_key)
    if cached:
        import_id = cached["response_ref"]
        return _import_detail(store, import_id, replayed=True)

    # 显式指定版本 -> 旧批次重跑绑定历史版本，不随新映射漂移
    forced_id = payload.get("mapping_version_id")
    if forced_id:
        version = store.get_version(forced_id)
        if version["status"] != "ACTIVE":
            raise DomainError("MAPPING_NOT_ACTIVE",
                              f"映射版本 {forced_id} 非生效状态，不能采信")
    else:
        version = store.active_for_batch(batch)
        if not version:
            raise DomainError("MAPPING_NOT_FOUND",
                              f"批次 {batch} 没有生效中的合并映射，请先登记")

    entries = store.entries_for(version["id"])
    current_codes = core.current_new_codes(entries)

    raw_rows = payload.get("rows") or []
    parsed_rows: list[dict] = []
    parse_bad: list[dict] = []
    for line, raw in enumerate(raw_rows, 2):
        try:
            parsed_rows.append(core.parse_budget_row(raw, line))
        except (KeyError, ValueError) as exc:
            parse_bad.append({
                "source_line": line,
                "project_id": str((raw or {}).get("project_id", "")),
                "reason": str(exc), "raw": raw,
            })

    ledger, bind_bad = core.bind_budget_rows(
        parsed_rows, entries, batch, current_codes)
    quarantined = parse_bad + bind_bad

    digest_payload = {
        "batch": batch, "mapping_version": version["id"],
        "rows": [
            {k: (str(v) if isinstance(v, Decimal) else v)
             for k, v in r.items()}
            for r in parsed_rows
        ],
    }
    digest = canonical_digest(digest_payload)

    existing = store.find_import_by_digest(batch, digest)
    if existing:  # 重试不重复入账
        return _import_detail(store, existing["id"], replayed=True)

    try:
        imp = store.post_import(batch, version["id"], digest, ledger,
                                quarantined, request_key)
    except WriteConflict as exc:
        raise _conflict(exc)
    return _import_detail(store, imp["id"], replayed=False)


def _entry_view(store: MergeStore, version_id: str) -> list[dict]:
    return [
        {**e, "weight": str(e["weight"])}
        for e in store.entries_for(version_id)
    ]


def _import_detail(store: MergeStore, import_id: str,
                   replayed: bool) -> dict:
    imp = store.get_import(import_id)
    ledger = store.ledger_for(import_id)
    version = (store.get_version(imp["mapping_version_id"])
               if imp["mapping_version_id"] else None)
    quarantine = store.quarantine_for("budget_import", import_id)
    return {
        "status": "IDEMPOTENT_REPLAY" if replayed else "POSTED",
        "replayed": replayed,
        "import": imp,
        "mapping_version": version,
        "ledger": ledger,
        "quarantine": quarantine,
        # 隔离行不占有效合计：合计仅累加台账金额
        "valid_total": imp["valid_total"],
        "input_count": imp["valid_count"] + imp["quarantine_count"],
        "valid_count": imp["valid_count"],
        "quarantine_count": imp["quarantine_count"],
    }


# ---------- 权重测算：与采信绑定同一版本 ----------
def calculate_weights(store: MergeStore, payload: dict) -> dict:
    forced_id = payload.get("mapping_version_id")
    if forced_id:
        version = store.get_version(forced_id)
        batch = payload.get("batch") or version["effective_batch"]
    else:
        batch = core.batch_text(payload.get("batch"))
        version = store.active_for_batch(batch)
        if not version:
            raise DomainError("MAPPING_NOT_FOUND",
                              f"批次 {batch} 没有生效中的合并映射")
    if version["status"] != "ACTIVE":
        raise DomainError("MAPPING_NOT_ACTIVE",
                          f"映射版本 {version['id']} 非生效状态")

    import_id = payload.get("import_id")
    if import_id:
        imp = store.get_import(import_id)
        if imp["mapping_version_id"] != version["id"]:
            raise DomainError(
                "VERSION_MISMATCH",
                "预算采信与权重测算必须绑定同一映射版本")
        ledger = store.ledger_for(import_id)
        for row in ledger:  # Decimal 化供纯函数计算
            row["weight"] = Decimal(row["weight"])
            row["amount"] = Decimal(row["amount"])
            row["cap"] = Decimal(row["cap"])
    else:
        # 无导入单号时直接用登记权重按新编码归集
        entries = store.entries_for(version["id"])
        ledger = [
            {"project_id": e["old_code"],
             "claimed_entity": e["old_entity"],
             "mapped_new_code": e["new_code"],
             "new_entity": e["new_entity"], "is_legacy": True,
             "amount": Decimal("0.00"), "weight": e["weight"],
             "cap": Decimal("0.00"), "cap_source": e["old_code"],
             "source_line": e["source_line"]}
            for e in entries
        ]

    groups = core.aggregate_weights(ledger)
    out_groups = []
    for g in groups:
        out_groups.append({
            "new_code": g["new_code"], "new_entity": g["new_entity"],
            "weight": str(g["weight"]), "share": str(g["share"]),
            "amount": str(g["amount"]), "members": g["members"],
        })
    share_sum = sum((g["share"] for g in groups), Decimal(0))
    return {
        "status": "READY", "batch": batch,
        "mapping_version_id": version["id"],
        "mapping_version_no": version["version"],
        "effective_batch": version["effective_batch"],
        "group_count": len(groups),
        "groups": out_groups,
        "share_sum": str(share_sum.normalize()),
        "reason": "OK",
    }


# ---------- 追查：合并前后归属、权重、隔离 ----------
def trace(store: MergeStore, payload: dict | None = None) -> dict:
    payload = payload or {}
    versions = store.list_versions()
    imports = store.list_imports()
    detail = []
    for imp in imports[:20]:
        version = (store.get_version(imp["mapping_version_id"])
                   if imp["mapping_version_id"] else None)
        detail.append({
            "import": imp,
            "mapping_version": version,
            "ledger": store.ledger_for(imp["id"]),
            "quarantine": store.quarantine_for("budget_import", imp["id"]),
        })
    mapping_quarantine = [
        q for q in store.all_quarantine() if q["scope"] == "mapping_publish"
    ]
    return {
        "status": "READY",
        "versions": versions,
        "imports": detail,
        "mapping_quarantine": mapping_quarantine,
        "interrupted": store.list_interrupted(),
        "filter_code": payload.get("code"),
    }
