# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |
| `mapping_version_id` | string | 否 | 预算采信与权重测算共同绑定的映射版本，旧批次重跑不漂移 |
| `effective_batch` | string | 否 | 映射生效批次（YYYY-MM），区间左闭右开 |
| `claimed_entity` | string | 否 | 旧预算补传认领的原申报主体 |
| `cap_source` | string | 否 | 封顶来源标识，合并后仍可追到原项目 |
| `request_key` | string | 是 | 发布/采信请求幂等键，同键重试不重复入账 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。

## M7 合并映射状态机

- 映射版本：`DRAFT`（条目已落库待激活）→ `ACTIVE`（生效）；同批次旧生效版本激活时置 `SUPERSEDED`；模拟或真实中断落 `INTERRUPTED`，经 `/api/mergers/mappings/recover` 恢复为 `ACTIVE`。
- 预算导入：仅 `POSTED` 一种终态；同批次同输入摘要或同 `request_key` 的重试返回原导入单。
- 隔离记录独立于有效台账：`DUPLICATE_CONFLICT`（同号重复）、`UNMAPPED_CODE`（未登记编码）、`EMPTY_VALUE`、`NEGATIVE_WEIGHT` 等只入 `quarantine_records`，不参与 `valid_total` 合计。
