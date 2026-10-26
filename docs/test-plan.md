# 测试说明与需求追踪

> 历史说明：本文引用的 evidence 文件、截图和日志仅本机保管，不随 Git 仓库分发；历史测试结果不代表本次或云端验证。

2026-10-22，L3，全过程开发。编码前计划位于[实施契约](implementation-contract.md)，详细独立用例及缺陷见[独立测试报告](independent-test-report.md)，最终命令和结果见[交付验收记录](delivery-report.md)。

## 隔离原则

仅使用本项目新建PostgreSQL和专用standalone_api_test、standalone_query_test、standalone_ingest_test、standalone_independent_test。各开发包独占自己的测试库，允许并行但不可共用清理库。E2E另用muyaju-data-service-e2e容器项目和独立数据库卷，主8090服务无合成业务数据。真实第三方、旧看板数据库、生产库均不参与。

测试对象是真实金额类型、动态DDL、SQL查询、鉴权、事务、worker和PostgreSQL。HTTP故障使用受控本地测试源或MockTransport隔离第三方边界，不能证明真实供应商授权与数据正确性。容器链路使用真实TCP HTTP和两个独立协议源。

## 追踪矩阵

| 需求 | 工作包/里程碑 | 实现 | 自动化及证据 |
| --- | --- | --- | --- |
| R01 独立项目 | WP1/M1-M3 | Dockerfile、Compose、api/cli/config/db | test_api独立路由/构建依赖；Docker镜像/启动核对 |
| R02 多数据集 | WP2/M2 | models/datasets | test_datasets；独立两异构schema及注册并发 |
| R03 数据质量 | WP2/M2 | datasets.validate_value/validate_record | 正负NUMERIC边界、NULL/日期/时间/整数/布尔/前导零/主键/未知字段 |
| R04 推送与原子发布 | WP3/M2 | ingest | 幂等、分片冲突、非法/重复/并发、行数/日期、取消、保旧、覆盖 |
| R05 HTTP采集 | WP3/M2-M3 | source/worker | 参数名/位置/映射/转换/签名/短页/空页/HTTP失败/重试/限速/大小；Docker两源 |
| R06 范围与聚合 | WP2/M2 | query | 单/多维、全部聚合操作、NULL、排序分页、缺日/空日、同快照批量、注入/预算 |
| R07 授权 | WP1/M2 | auth/access/api | 匿名、跨表、只读、所有权、过期/撤销/限频、reset、密码及请求不回显 |
| R08 运行与恢复 | WP3/M2-M3 | worker/ingest | 独占锁、真实进程kill、失败保旧、重启清暂存、保留和24h清理、心跳 |
| R09 部署与恢复 | WP1/M3 | Docker和snapshot/verify脚本 | 非root镜像、真容器停启、8表恢复SHA、重复恢复拒绝、镜像save/load |
| R10 独立闭环 | WP4/M4 | tests/independent | 最终67项、代码执行前后SHA一致、缺陷交实现者修复复验 |

## 边界与准入退出

静态检查和所有针对性测试先通过，再进行整体Docker链路、恢复、独立审查。失败保留证据，不删除/降断言/假称通过。无未关闭P0/P1、关键路径通过后才可判本地候选通过；CI、人工代码评审、业务对账、生产发布分别记状态。

性能样本包含1日4万事实、100分组、179个空日，以及5并发的1/30/180天查询；仅记录本机条件下结果，不代表真实720万行、数据库RSS、最坏字段宽度或服务器最低配置。大文本测试实际触发8MiB单响应和16MiB批量限制，并确认缩减投影可查。

安全范围覆盖身份、越权、SQL标识符/值注入、重复JSON、正文超限、数据源主机约束/重定向、错误脱敏。没有文件上传、OS命令API或任意脚本解释；1.2.0增加受限算术表达式，需专项验证注入、类型、复杂度、除零和Decimal精度（见evidence/formula-plan.json），文件上传专项不适用；不声称覆盖生产网络渗透。Python依赖审计不覆盖PostgreSQL/基础镜像操作系统漏洞。

## 可复现命令

先创建专用库并在本地私密`.local/test-*.env`中设置DS_TEST_DATABASE_URL。测试会清理其专用库，绝不能填应用或生产连接地址。

```powershell
uv sync --frozen
uv run ruff check data_service scripts tests
uv run --env-file .local/test-api.env pytest tests/test_api.py -q
uv run --env-file .local/test-query.env pytest tests/test_datasets.py tests/test_query.py -q
uv run --env-file .local/test-ingest.env pytest tests/test_ingest.py tests/test_source.py -q
uv run --env-file .local/test-independent.env pytest tests/independent -q
uv run python scripts/prepare-e2e.py
docker compose --env-file .local/e2e.env -f tests/e2e/compose.yml up -d --wait
uv run python scripts/verify-docker.py
docker compose --env-file .local/e2e.env -f tests/e2e/compose.yml stop
```

复现E2E需先构建本项目镜像；测试配置和数据都仅在专用环境。测试保留可识别的库和卷供审计，停止不删卷。需要清理时必须逐项确认专用资源，不执行全局Docker清理。

## 1.3.0 配置助手测试追踪

本轮全过程开发、L3（新增加密配置存储、外部模型调用和公式配置）。编码前计划见`evidence/assistant-plan.json`，执行结果见`evidence/assistant-delivery.json`，各子报告保留命令/退出码/数量及隔离边界。

| 编号 | 需求及关键验收 | 层级/数据 |
| --- | --- | --- |
| N01 | 批量求和、点选已有汇总、光标插入、比率助手、手工配置兼容 | 真实Edge自动化，API桩只隔离传输 |
| N02 | 自然语言草稿预览后应用，多维组合、日期参数、退款口径澄清 | 真实PG与受控本地HTTP，UI |
| N03 | 管理员权限、KEY加密不回显、改目标保护、坏草稿与注入拒绝 | 单元/API/真实HTTP |
| N04 | 模型配置revision、保留/替换/清除、并发和慢响应不覆盖 | API并发、UI |
| N05 | 超时、限流、重定向、巨响应、无效JSON、失败后恢复、不自动重试 | 受控HTTP专项 |
| N06 | schema3→4原子迁移，旧版备份恢复，原业务状态指纹不变 | 隔离库、实际备份恢复副本 |
| N07 | 完整镜像、本地运行、重启、查询回归、离线交付包 | Docker、真实只读查询、静态检查 |

测试不向真实模型发送业务记录，不改真实供应商凭据；新模型协议使用本地可控响应服务。它验证系统协议和安全边界，不能验证某供应商的模型生成质量或付费账户能力。真实模型未配置时明确标记未验证。人工代码评审、CI、业务人员最终确认及生产发布仍单独记录，不能由自动测试代替。
