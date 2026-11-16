# 深度登记 专项资金分摊与舍入尾差复核

        - 规模：中型
        - 核心规则：先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。
        - 使用者与工作流：导入随仓样例，逐模块复核，最后导出单一证据清单。

        ## 功能模块

        | 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
        |---|---|---|---|---|
        | M1 预算采信 | 运行样例并复核证据 | `/api/budgets/import` / `/budgets` | `app/modules/m1_budgets/core.py` `app/modules/m1_budgets/service.py` | `tests/test_m1_budgets.py` |
| M2 资格校验 | 运行样例并复核证据 | `/api/eligibility/evaluate` / `/eligibility` | `app/modules/m2_eligibility/core.py` `app/modules/m2_eligibility/service.py` | `tests/test_m2_eligibility.py` |
| M3 权重测算 | 运行样例并复核证据 | `/api/weights/calculate` / `/weights` | `app/modules/m3_weights/core.py` `app/modules/m3_weights/service.py` | `tests/test_m3_weights.py` |
| M4 金额分摊 | 运行样例并复核证据 | `/api/allocations/calculate` / `/allocations` | `app/modules/m4_allocations/core.py` `app/modules/m4_allocations/service.py` | `tests/test_m4_allocations.py` |
| M5 尾差裁决 | 运行样例并复核证据 | `/api/remainders/resolve` / `/remainders` | `app/modules/m5_remainders/core.py` `app/modules/m5_remainders/service.py` | `tests/test_m5_remainders.py` |
| M6 批次复核 | 运行样例并复核证据 | `/api/export/allocations.csv` / `/runs` | `app/modules/m6_runs/core.py` `app/modules/m6_runs/service.py` | `tests/test_m6_runs.py` |
| M7 合并映射 | 登记带生效批次的合并映射、预算采信、权重测算与追查 | `/api/mergers/mappings/register` 等 / `/budgets#mergers` | `app/modules/m7_mergers/core.py` `app/modules/m7_mergers/repository.py` `app/modules/m7_mergers/service.py` | `tests/test_m7_mergers_core.py` `tests/test_m7_mergers_api.py` |

        ## 陷阱登记

        | TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
        |---|---|---|---|---|---|
        | TRAP-05 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_budgets/core.py` | `tests/test_m1_budgets.py::test_trap_1` |
| TRAP-06 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_eligibility/core.py` | `tests/test_m2_eligibility.py::test_trap_1` |
| TRAP-07 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_weights/core.py` | `tests/test_m3_weights.py::test_trap_1` |
| TRAP-09 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_allocations/core.py` | `tests/test_m4_allocations.py::test_trap_1` |
| TRAP-10 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m5_remainders/core.py` | `tests/test_m5_remainders.py::test_trap_1` |
| TRAP-14 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m6_runs/core.py` | `tests/test_m6_runs.py::test_trap_1` |
| TRAP-19 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_budgets/core.py` | `tests/test_m1_budgets.py::test_trap_2` |
| TRAP-22 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_eligibility/core.py` | `tests/test_m2_eligibility.py::test_trap_2` |
| TRAP-24 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_weights/core.py` | `tests/test_m3_weights.py::test_trap_2` |
| TRAP-26 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_allocations/core.py` | `tests/test_m4_allocations.py::test_trap_2` |
| TRAP-31 | 换版后直接套新映射会串账，沿用旧映射会拆开新项目 | 按编码单表覆盖 | 带生效批次的版本化映射：左闭右开解析，旧批次重跑绑定历史版本不漂移；同号不同批次可属不同主体 | `app/modules/m7_mergers/core.py` `app/modules/m7_mergers/repository.py` | `tests/test_m7_mergers_api.py::test_old_batch_rerun_never_drifts` |
| TRAP-32 | 映射切换中断后状态丢失 | 单阶段直接覆盖 | 两阶段发布：DRAFT/INTERRUPTED→ACTIVE，中断版本可恢复，恢复前不产生 ACTIVE | `app/modules/m7_mergers/repository.py` | `tests/test_m7_mergers_api.py::test_interrupted_publish_recoverable` |
| TRAP-33 | 并发发布后写覆盖 | 先查后写无锁 | 版本号自增与乐观锁复检置于同一 `BEGIN IMMEDIATE` 事务，过期版本 422 拒绝 | `app/modules/m7_mergers/repository.py` | `tests/test_m7_mergers_api.py::test_concurrent_publish_only_one_wins` |
| TRAP-34 | 重试导致重复入账 | 每次插入新行 | 输入摘要 + 幂等键双重去重，重试复用原导入单，台账行不新增 | `app/modules/m7_mergers/repository.py` | `tests/test_m7_mergers_api.py::test_retry_does_not_double_post` |
| TRAP-35 | 合并后原项目金额与封顶来源丢失 | 只留新编码汇总 | 台账逐行保留原编码、认领主体、金额、封顶与封顶来源，汇总组内嵌成员明细 | `app/modules/m7_mergers/core.py` | `tests/test_m7_mergers_core.py::test_aggregate_weights_and_member_trace` |
| TRAP-36 | 冲突记录污染有效合计 | 冲突行参与求和 | 同号重复、未映射等行入隔离表独立统计，有效合计仅累加台账金额 | `app/modules/m7_mergers/repository.py` | `tests/test_m7_mergers_api.py::test_import_binds_version_and_keeps_ledger_trace` |

        ## 对账规则

        - 输入记录数 = 有效记录数 + 隔离记录数 + 空记录数。
        - 核心数值或事件数在转换、判定和导出之间保持守恒。
        - 同一输入摘要重跑复用原运行编号，不增加结果行。
        - M7 预算采信与权重测算必须绑定同一 `mapping_version_id`，版本不一致以 `VERSION_MISMATCH` 拒绝；隔离记录不占有效合计。

        ## 已知不覆盖

        - 生产系统连接、身份认证和外部密钥不在演示范围内。
