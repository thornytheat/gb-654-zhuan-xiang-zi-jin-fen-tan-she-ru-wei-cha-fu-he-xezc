REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    "EMPTY_BATCH": "生效批次为空", "INVALID_BATCH": "生效批次格式应为 YYYY-MM",
    "INVALID_WEIGHT": "权重值不符合定义", "NEGATIVE_WEIGHT": "权重为负",
    "UNMAPPED_CODE": "项目编码未登记合并映射",
    "MAPPING_NOT_FOUND": "批次无生效映射", "MAPPING_NOT_ACTIVE": "映射版本未生效",
    "VERSION_MISMATCH": "采信与测算绑定版本不一致",
    "VERSION_NOT_RESUMABLE": "版本状态不允许恢复",
    "WRITE_CONFLICT": "并发写入冲突",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")
