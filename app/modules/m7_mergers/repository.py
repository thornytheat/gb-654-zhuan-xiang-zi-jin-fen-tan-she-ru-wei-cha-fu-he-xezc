"""合并映射域的 SQLite 仓储。

与 app.store.Store 共用同一个数据库文件，独立建表：
- mapping_versions   映射版本（草稿/中断/生效），含乐观锁 version
- mapping_entries    各版本的有效映射行
- budget_imports     预算导入批次头，绑定映射版本，保证重跑不漂移
- budget_entries     入账台账行（原项目金额与封顶来源逐行保留）
- quarantine_records 冲突/隔离记录，独立统计，不进入有效合计
- idempotency_keys   发布与导入的请求幂等键
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WriteConflict(Exception):
    """SQLite 写竞争（database is locked）。"""


class StaleVersion(Exception):
    """乐观锁失败：请求基于的版本号已过期。"""


class MergeStore:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS mapping_versions (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL UNIQUE,
                    effective_batch TEXT NOT NULL,
                    note TEXT,
                    status TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    valid_count INTEGER NOT NULL,
                    quarantine_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS mapping_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mapping_version_id TEXT NOT NULL REFERENCES mapping_versions(id),
                    source_line INTEGER NOT NULL,
                    old_code TEXT NOT NULL,
                    old_entity TEXT NOT NULL,
                    new_code TEXT NOT NULL,
                    new_entity TEXT NOT NULL,
                    weight TEXT NOT NULL,
                    UNIQUE(mapping_version_id, old_code)
                );
                CREATE TABLE IF NOT EXISTS budget_imports (
                    id TEXT PRIMARY KEY,
                    batch TEXT NOT NULL,
                    mapping_version_id TEXT REFERENCES mapping_versions(id),
                    digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    valid_count INTEGER NOT NULL,
                    quarantine_count INTEGER NOT NULL,
                    valid_total TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    import_id TEXT NOT NULL REFERENCES budget_imports(id),
                    source_line INTEGER NOT NULL,
                    project_id TEXT NOT NULL,
                    claimed_entity TEXT NOT NULL,
                    mapped_new_code TEXT NOT NULL,
                    new_entity TEXT NOT NULL,
                    is_legacy INTEGER NOT NULL,
                    amount TEXT NOT NULL,
                    weight TEXT NOT NULL,
                    cap TEXT NOT NULL,
                    cap_source TEXT NOT NULL,
                    UNIQUE(import_id, project_id)
                );
                CREATE TABLE IF NOT EXISTS quarantine_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    ref_id TEXT NOT NULL,
                    source_line INTEGER NOT NULL,
                    business_key TEXT,
                    reason TEXT NOT NULL,
                    raw TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    request_key TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    response_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entries_version ON mapping_entries(mapping_version_id);
                CREATE INDEX IF NOT EXISTS idx_budget_import_batch ON budget_imports(batch);
                CREATE INDEX IF NOT EXISTS idx_quarantine_ref ON quarantine_records(scope, ref_id);
                """
            )

    # ---------- 通用 ----------
    @staticmethod
    def _rows(sql: str, params: tuple, db: sqlite3.Connection) -> list[dict]:
        return [dict(r) for r in db.execute(sql, params).fetchall()]

    def cached_response(self, request_key: str | None) -> dict | None:
        if not request_key:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT response_ref FROM idempotency_keys WHERE request_key=?",
                (request_key,),
            ).fetchone()
        if not row:
            return None
            # 调用方按 response_ref 回查完整结果
        return {"response_ref": row[0]}

    def remember_key(self, request_key: str | None, scope: str,
                     response_ref: str, db: sqlite3.Connection) -> None:
        if not request_key:
            return
        db.execute(
            "INSERT OR IGNORE INTO idempotency_keys VALUES (?,?,?,?)",
            (request_key, scope, response_ref, _now()),
        )

    # ---------- 映射发布（两阶段，可恢复） ----------
    def begin_publish(self, effective_batch: str, digest: str,
                      valid_count: int, quarantine_count: int, note: str | None,
                      interrupt: bool,
                      expected_version: int | None = None) -> dict:
        """阶段一：建 DRAFT（或模拟中断时落 INTERRUPTED）。

        版本号自增与乐观锁复检在同一个立即事务内完成：拿到写锁后再读
        MAX(version)，并发发布中后到的请求在此处被 STALE_VERSION 拒绝。
        写竞争统一抛 WriteConflict 供服务层转 409。
        """
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT COALESCE(MAX(version),0) AS v FROM mapping_versions"
                ).fetchone()
                current_max = row["v"]
                if (expected_version is not None
                        and int(expected_version) != current_max):
                    raise StaleVersion(
                        f"提交版本已过期：当前最新版本号 {current_max}，"
                        f"请求基于 {expected_version}")
                next_version = current_max + 1
                version_id = f"map-{effective_batch}-v{next_version}"
                status = "INTERRUPTED" if interrupt else "DRAFT"
                db.execute(
                    "INSERT INTO mapping_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (version_id, next_version, effective_batch, note, status,
                     digest, valid_count, quarantine_count, _now(), None),
                )
                return self._version_by_id(version_id, db)
        except StaleVersion:
            raise
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise WriteConflict(str(exc))
            raise

    def insert_entries(self, version_id: str, entries: list[dict]) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT INTO mapping_entries(mapping_version_id,source_line,"
                "old_code,old_entity,new_code,new_entity,weight) VALUES (?,?,?,?,?,?,?)",
                [
                    (version_id, e["source_line"], e["old_code"], e["old_entity"],
                     e["new_code"], e["new_entity"], str(e["weight"]))
                    for e in entries
                ],
            )

    def insert_quarantine(self, scope: str, ref_id: str,
                          rows: list[dict]) -> None:
        if not rows:
            return
        with self.connect() as db:
            db.executemany(
                "INSERT INTO quarantine_records(scope,ref_id,source_line,"
                "business_key,reason,raw,created_at) VALUES (?,?,?,?,?,?,?)",
                [
                    (scope, ref_id, q["source_line"],
                     q.get("old_code") or q.get("project_id", ""),
                     q["reason"], json.dumps(q.get("raw"), ensure_ascii=False,
                                              default=str), _now())
                    for q in rows
                ],
            )

    def activate(self, version_id: str, request_key: str | None) -> dict:
        """阶段二：把指定版本置 ACTIVE，同批次旧 ACTIVE 置 SUPERSEDED。

        中断版本在恢复时也走这里；重复激活同一版本直接幂等返回。
        """
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                version = self._version_by_id(version_id, db)
                if version["status"] == "ACTIVE":
                    self.remember_key(request_key, "mapping_publish",
                                      version_id, db)
                    return version
                if version["status"] not in ("DRAFT", "INTERRUPTED"):
                    raise ValueError("VERSION_NOT_RESUMABLE")
                db.execute(
                    "UPDATE mapping_versions SET status='SUPERSEDED' "
                    "WHERE status='ACTIVE' AND effective_batch=?",
                    (version["effective_batch"],),
                )
                db.execute(
                    "UPDATE mapping_versions SET status='ACTIVE', activated_at=? "
                    "WHERE id=?",
                    (_now(), version_id),
                )
                self.remember_key(request_key, "mapping_publish", version_id, db)
                return self._version_by_id(version_id, db)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise WriteConflict(str(exc))
            raise

    def recover_interrupted(self) -> dict | None:
        """找到最近一个中断版本，补齐条目校验后置 ACTIVE，完成切换。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM mapping_versions WHERE status='INTERRUPTED' "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return self.activate(row["id"], None)

    def list_interrupted(self) -> list[dict]:
        with self.connect() as db:
            return self._rows(
                "SELECT * FROM mapping_versions WHERE status='INTERRUPTED' "
                "ORDER BY version", (), db)

    def _version_by_id(self, version_id: str,
                       db: sqlite3.Connection) -> dict:
        row = db.execute(
            "SELECT * FROM mapping_versions WHERE id=?", (version_id,)
        ).fetchone()
        if not row:
            raise ValueError("MAPPING_NOT_FOUND")
        return dict(row)

    def get_version(self, version_id: str) -> dict:
        with self.connect() as db:
            return self._version_by_id(version_id, db)

    def entries_for(self, version_id: str) -> list[dict]:
        with self.connect() as db:
            rows = self._rows(
                "SELECT source_line,old_code,old_entity,new_code,new_entity,weight "
                "FROM mapping_entries WHERE mapping_version_id=? ORDER BY source_line",
                (version_id,), db)
        for r in rows:
            r["weight"] = Decimal(r["weight"])
        return rows

    def list_versions(self) -> list[dict]:
        with self.connect() as db:
            return self._rows(
                "SELECT * FROM mapping_versions ORDER BY version DESC", (), db)

    def active_for_batch(self, batch: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM mapping_versions WHERE status='ACTIVE' "
                "AND effective_batch<=? ORDER BY effective_batch DESC, version DESC "
                "LIMIT 1", (batch,)).fetchone()
        return dict(row) if row else None

    # ---------- 预算导入（幂等入账） ----------
    def find_import_by_digest(self, batch: str, digest: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM budget_imports WHERE batch=? AND digest=?",
                (batch, digest)).fetchone()
        return dict(row) if row else None

    def post_import(self, batch: str, mapping_version_id: str | None,
                    digest: str, ledger: list[dict],
                    quarantined: list[dict], request_key: str | None) -> dict:
        import_id = f"imp-{digest[:12]}"
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT id FROM budget_imports WHERE id=? OR (batch=? AND digest=?)",
                    (import_id, batch, digest)).fetchone()
                if existing:  # 重试：不重复入账，直接回原编号
                    self.remember_key(request_key, "budget_import",
                                      existing["id"], db)
                    return self._import_by_id(existing["id"], db)
                valid_total = sum((r["amount"] for r in ledger), Decimal(0))
                db.execute(
                    "INSERT INTO budget_imports VALUES (?,?,?,?,?,?,?,?,?)",
                    (import_id, batch, mapping_version_id, digest, "POSTED",
                     len(ledger), len(quarantined), str(valid_total), _now()),
                )
                db.executemany(
                    "INSERT INTO budget_entries(import_id,source_line,project_id,"
                    "claimed_entity,mapped_new_code,new_entity,is_legacy,amount,"
                    "weight,cap,cap_source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (import_id, r["source_line"], r["project_id"],
                         r["claimed_entity"], r["mapped_new_code"],
                         r["new_entity"], 1 if r["is_legacy"] else 0,
                         str(r["amount"]), str(r["weight"]),
                         str(r["cap"]), r["cap_source"])
                        for r in ledger
                    ],
                )
                if quarantined:
                    db.executemany(
                        "INSERT INTO quarantine_records(scope,ref_id,source_line,"
                        "business_key,reason,raw,created_at) VALUES (?,?,?,?,?,?,?)",
                        [
                            ("budget_import", import_id, q["source_line"],
                             q.get("project_id", ""), q["reason"],
                             json.dumps(q.get("raw"), ensure_ascii=False, default=str),
                             _now())
                            for q in quarantined
                        ],
                    )
                self.remember_key(request_key, "budget_import", import_id, db)
                return self._import_by_id(import_id, db)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise WriteConflict(str(exc))
            raise

    def _import_by_id(self, import_id: str,
                      db: sqlite3.Connection) -> dict:
        return dict(db.execute(
            "SELECT * FROM budget_imports WHERE id=?", (import_id,)).fetchone())

    def get_import(self, import_id: str) -> dict:
        with self.connect() as db:
            return self._import_by_id(import_id, db)

    def ledger_for(self, import_id: str) -> list[dict]:
        with self.connect() as db:
            rows = self._rows(
                "SELECT * FROM budget_entries WHERE import_id=? ORDER BY source_line",
                (import_id,), db)
        for r in rows:
            r["is_legacy"] = bool(r["is_legacy"])
        return rows

    def list_imports(self) -> list[dict]:
        with self.connect() as db:
            return self._rows(
                "SELECT * FROM budget_imports ORDER BY created_at DESC", (), db)

    def quarantine_for(self, scope: str, ref_id: str) -> list[dict]:
        with self.connect() as db:
            return self._rows(
                "SELECT source_line,business_key,reason,raw,created_at "
                "FROM quarantine_records WHERE scope=? AND ref_id=? ORDER BY id",
                (scope, ref_id), db)

    def all_quarantine(self) -> list[dict]:
        with self.connect() as db:
            return self._rows(
                "SELECT * FROM quarantine_records ORDER BY id DESC", (), db)
