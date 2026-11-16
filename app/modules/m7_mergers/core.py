"""合并映射与批次采信核心规则（纯函数，不依赖持久化）。

业务背景：项目编码换版后两个旧项目合并为新编码。新申报按新编码归集权重，
旧预算补传仍按原申报主体认领。映射必须带生效批次，使预算导入与权重测算
绑定同一映射版本；同一旧编码在不同批次可归属不同主体；合并后各原项目的
金额与封顶来源逐行保留，冲突行隔离且不进入有效合计。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.manifest import canonical_digest
from app.modules.m1_budgets.core import NULLS, money

SAMPLE = {
    "effective_batch": "2026-11",
    "note": "编码换版合并样例",
    "entries": [
        {"old_code": "000101", "old_entity": "城北公路段",
         "new_code": "X2026001", "new_entity": "市政交通中心", "weight": "2"},
        {"old_code": "000102", "old_entity": "城南养护站",
         "new_code": "X2026001", "new_entity": "市政交通中心", "weight": "1"},
        {"old_code": "9007199254740993", "old_entity": "桥隧所",
         "new_code": "X2026002", "new_entity": "市政交通中心", "weight": "1"},
        # 同号在本批次重复登记 -> 隔离，不占有效合计
        {"old_code": "000101", "old_entity": "城北公路段",
         "new_code": "X2026009", "new_entity": "其他主体", "weight": "1"},
    ],
}

# 隔离原因码（同步登记到 app/reasons.py）
REQUIRED_FIELDS = ("old_code", "old_entity", "new_code", "new_entity")


def batch_text(value) -> str:
    """生效批次统一为左闭右开区间使用的 YYYY-MM 文本。"""
    text = str(value).strip()
    if text in NULLS:
        raise ValueError("EMPTY_BATCH")
    parts = text.split("-")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise ValueError("INVALID_BATCH")
    year, month = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12) or year < 1900 or year > 9999:
        raise ValueError("INVALID_BATCH")
    return f"{year:04d}-{month:02d}"


def entry_weight(value) -> Decimal:
    if value is None or str(value).strip() in NULLS:
        return Decimal(1)  # 登记时权重可缺省，缺省记 1
    try:
        weight = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("INVALID_WEIGHT")
    if weight < 0:
        raise ValueError("NEGATIVE_WEIGHT")
    return weight


def parse_entries(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """解析合并映射登记行，返回(有效行, 隔离行)。

    同一批次内同号重复登记属于冲突，第二行起隔离，保留原始证据。
    输入顺序变化不得改变裁决：按物理行号先到先得。
    """
    good: list[dict] = []
    quarantined: list[dict] = []
    seen: set[str] = set()
    for line, raw in enumerate(entries, 2):
        row = dict(raw or {})
        try:
            for field in REQUIRED_FIELDS:
                if str(row.get(field, "")).strip() in NULLS:
                    raise ValueError("EMPTY_VALUE")
            weight = entry_weight(row.get("weight"))
        except ValueError as exc:
            quarantined.append({
                "source_line": line, "reason": str(exc),
                "old_code": str(row.get("old_code", "")), "raw": row,
            })
            continue
        old_code = str(row["old_code"]).strip()
        if old_code in seen:
            quarantined.append({
                "source_line": line, "reason": "DUPLICATE_CONFLICT",
                "old_code": old_code, "raw": row,
            })
            continue
        seen.add(old_code)
        good.append({
            "source_line": line,
            "old_code": old_code,
            "old_entity": str(row["old_entity"]).strip(),
            "new_code": str(row["new_code"]).strip(),
            "new_entity": str(row["new_entity"]).strip(),
            "weight": weight,
        })
    return good, quarantined


def mapping_digest(entries: list[dict]) -> str:
    """对有效映射行做规范摘要，用于同内容幂等与版本比对。"""
    payload = [
        {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()
         if k != "source_line"}
        for row in sorted(entries, key=lambda x: x["old_code"])
    ]
    return canonical_digest(payload)


def resolve_version(versions: list[dict], batch: str) -> dict | None:
    """按生效批次左闭右开解析当前适用版本。

    只在 ACTIVE 版本中选择 effective_batch <= batch 的最新一条；
    显式传入 mapping_version_id 时由服务层直接取该版本，保证旧批次
    重跑不随后续新映射漂移。
    """
    active = [
        v for v in versions
        if v["status"] == "ACTIVE" and v["effective_batch"] <= batch
    ]
    if not active:
        return None
    return max(active, key=lambda v: (v["effective_batch"], v["version"]))


def parse_budget_row(raw: dict, line: int) -> dict:
    """预算补传/新申报行解析，金额、权重、封顶沿用 M1 口径。"""
    row = dict(raw or {})
    amount = money(row["amount"], row.get("unit", "元"))
    weight = entry_weight(row.get("weight"))
    cap = money(row["cap"])
    code = str(row["project_id"]).strip()
    if code in NULLS:
        raise ValueError("EMPTY_VALUE")
    cap_source = str(row.get("cap_source", "")).strip()
    return {
        "source_line": line,
        "project_id": code,
        "amount": amount,
        "weight": weight,
        "cap": cap,
        # 封顶来源缺省即原项目自身，合并后仍可追到原编码
        "cap_source": cap_source or code,
        "entity_hint": str(row.get("entity", "")).strip(),
    }


def bind_budget_rows(rows: list[dict], entries: list[dict], batch: str,
                     current_codes: set[str]) -> tuple[list[dict], list[dict]]:
    """把预算行与指定版本的映射绑定，输出有效台账行与隔离行。

    - 旧编码补传：按映射认领到原申报主体(old_entity)，同时归集到新编码；
    - 新编码申报：直接属于新主体；
    - 映射查不到且不是现行新编码：UNMAPPED_CODE 隔离；
    - 同号重复入账：DUPLICATE_CONFLICT 隔离（重试场景由服务层幂等拦截）。
    """
    index = {e["old_code"]: e for e in entries}
    ledger: list[dict] = []
    quarantined: list[dict] = []
    seen: set[str] = set()
    for parsed in rows:
        line = parsed["source_line"]
        code = parsed["project_id"]
        if code in seen:
            quarantined.append({
                "source_line": line, "project_id": code,
                "reason": "DUPLICATE_CONFLICT", "raw": code,
            })
            continue
        if code in index:
            entry = index[code]
            bound = dict(parsed)
            bound.update({
                "claimed_entity": entry["old_entity"],
                "mapped_new_code": entry["new_code"],
                "new_entity": entry["new_entity"],
                "is_legacy": True,
            })
        elif code in current_codes:
            bound = dict(parsed)
            bound.update({
                "claimed_entity": parsed["entity_hint"] or code,
                "mapped_new_code": code,
                "new_entity": parsed["entity_hint"] or code,
                "is_legacy": False,
            })
        else:
            quarantined.append({
                "source_line": line, "project_id": code,
                "reason": "UNMAPPED_CODE", "raw": code,
            })
            continue
        seen.add(code)
        ledger.append(bound)
    return ledger, quarantined


def aggregate_weights(ledger: list[dict]) -> list[dict]:
    """按新编码归集权重并计算份额；保留各原项目金额与封顶来源。"""
    groups: dict[str, dict] = {}
    for row in ledger:
        key = row["mapped_new_code"]
        group = groups.setdefault(key, {
            "new_code": key,
            "new_entity": row["new_entity"],
            "weight": Decimal(0),
            "amount": Decimal(0),
            "members": [],
        })
        group["weight"] += row["weight"]
        group["amount"] += row["amount"]
        group["members"].append({
            "project_id": row["project_id"],
            "claimed_entity": row["claimed_entity"],
            "amount": str(row["amount"]),
            "weight": str(row["weight"]),
            "cap": str(row["cap"]),
            "cap_source": row["cap_source"],
            "is_legacy": row["is_legacy"],
        })
    total_weight = sum((g["weight"] for g in groups.values()), Decimal(0))
    if total_weight == 0:
        raise ValueError("ZERO_WEIGHT_GROUP")
    ordered = sorted(groups.values(), key=lambda g: g["new_code"])
    for group in ordered:
        group["share"] = group["weight"] / total_weight
    return ordered


def current_new_codes(entries: list[dict]) -> set[str]:
    return {e["new_code"] for e in entries}
