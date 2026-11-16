# 专项资金分摊与舍入尾差复核

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 预算采信：`/api/budgets/import`（页面内另含 M7 合并映射工作区 `/budgets#mergers`）
- M2 资格校验：`/api/eligibility/evaluate`
- M3 权重测算：`/api/weights/calculate`
- M4 金额分摊：`/api/allocations/calculate`
- M5 尾差裁决：`/api/remainders/resolve`
- M6 批次复核：`/api/export/allocations.csv`
- M7 合并映射：`/api/mergers/mappings/register`、`/api/mergers/budgets/import`、`/api/mergers/weights/calculate`、`/api/mergers/trace`

## 合并映射规则（M7）

编码换版后旧项目合并，映射按生效批次版本化登记，预算导入与权重测算绑定同一 `mapping_version_id`：

- 同号在不同批次可属不同主体；旧预算补传按原申报主体认领，新申报按新编码归集。
- 合并后逐行保留各原项目金额与封顶来源（`cap_source`）。
- 发布为两阶段（DRAFT/INTERRUPTED→ACTIVE），中断可经 `/api/mergers/mappings/recover` 恢复；旧批次重跑不随新映射漂移。
- 并发发布以乐观锁拒绝过期版本；输入摘要加幂等键保证重试不重复入账。
- 同号重复、未映射编码等冲突入隔离表，独立统计且不占有效合计；页面可追查合并前后归属与权重。

        ## 核心规则

        先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18213`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
