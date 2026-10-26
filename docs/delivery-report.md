# 独立数据服务交付与验收记录

> 历史说明：本文引用的 evidence 文件、截图和日志仅本机保管，不随 Git 仓库分发；历史测试结果不代表本次或云端验证。

日期：2026-10-22。执行模式：全过程开发。等级：L3，涉及数据库、金额精度、对外接口、鉴权和并发写入。AI自检结论：**本地候选有条件通过**。

## 一、任务闭环

本轮按用户纠正后的范围重新交付**独立多数据集API服务**，项目根目录为`D:\codex\慕雅居数据服务`。该目录不依赖原看板目录；未修改原Vue项目或原应用数据库。没有前端容器、RuoYi登录/菜单、经营看板路由。原看板仅作为另一个项目存在。

技术组成：FastAPI API、独立同步worker、PostgreSQL17、一次性初始化，Docker Compose独立卷和独立网络。本地入口`http://127.0.0.1:8090`，`/docs`为离线接口目录，`/openapi.json`为机器接口定义；这不是数据管理GUI。管理员私密凭据见`.local/admin-credentials.txt`。

目前API/数据库运行健康，worker运行且业务心跳健康。正式本地应用没有注册测试数据集、没有业务记录、没有真实源配置，自动调度关闭。合成数据只在专用测试库和E2E容器卷中；E2E验收后已停止，保留可识别卷供复现。未请求真实第三方、未向远程部署或推送镜像。

| 需求 | 里程碑/工作包 | 实现 | 用例及证据 | 本地结果 |
| --- | --- | --- | --- | --- |
| R01 独立项目 | M1/M3、WP1 | 独立目录、Docker/API/DB/初始化 | test_api隔离检查、镜像和主站manifest | 完成 |
| R02/R03 多表与类型 | M2、WP2 | models/datasets | 107项包测试中的注册、边界、DDL、精度；两异构表 | 通过 |
| R04/R05 接入处理 | M2/M3、WP3 | ingest/source/worker | 93项接入测试、真实Docker两个HTTP源 | 通过；真实源未验 |
| R06 开放查询 | M2、WP2 | query/API | 明细/分组/排序/范围/批量/快照/响应预算 | 通过 |
| R07 授权 | M2、WP1 | auth/access/API | 23项管理/API测试；独立越权、只读、所有权、限频 | 通过 |
| R08 运行与恢复 | M2/M3、WP3 | worker/ingest | 独立进程kill、锁、清理、心跳和容器重启 | 通过 |
| R09 部署材料 | M3/M5、WP1 | Docker及部署/恢复脚本 | 镜像save/load、非root、8关键表恢复指纹 | 本地通过 |
| R10 独立闭环 | M4、WP4 | 独立测试代理 | 最终67项；发现缺陷交查询代理修复再独立复验 | 有条件通过 |

主代理负责边界、鉴权/API、Docker与集成；查询代理负责类型与查询；接入代理负责采集/发布/worker；文档代理补接口与配置手册；独立测试代理不参与业务实现。关键路径与写入范围见[实施契约](implementation-contract.md)，详细追踪见[测试说明](test-plan.md)。

工作目录是新独立项目，未初始化Git，无法提供分支/提交/Git diff。旧项目只读复用已知来源的管理员鉴权逻辑，已改为本项目模块及配置；没有来源不明外部代码复制。最终候选源码SHA256见`evidence/delivery-manifest.json`，独立执行前后产品文件SHA一致。未commit、push、merge、rebase或清理其他项目。

## 二、实际测试证据

环境：Windows11、Python3.12.7、PostgreSQL17 Linux容器、Docker29.6.1/Compose5.3.0。服务依赖由`uv.lock`和`requirements.lock`固定，第三方版本/许可证元数据/PyPI来源记录在`evidence/dependencies.json`。

| 实际命令（本项目根目录） | 退出码 | 结果 |
| --- | --- | --- |
| `uv run ruff check data_service scripts tests` | 0 | 静态检查通过 |
| `uv run --env-file .local/test-api.env pytest tests/test_api.py -q --junitxml=evidence/api-final.xml` | 0 | 23通过，0失败/跳过 |
| `uv run --env-file .local/test-query.env pytest tests/test_datasets.py tests/test_query.py -q --junitxml=evidence/query-final.xml` | 0 | 107通过，0失败/跳过 |
| `uv run --env-file .local/test-ingest.env pytest tests/test_ingest.py tests/test_source.py -q --junitxml=evidence/ingest-final.xml` | 0 | 93通过，0失败/跳过 |
| 独立测试进程加载`.local/test-independent.env`后，`.venv/Scripts/python.exe -m pytest tests/independent -q --junitxml=evidence/independent/final-suite.xml` | 0 | 67通过，0失败/错误/跳过 |
| `docker compose build api` | 0 | 业务镜像构建成功，日志`evidence/docker-build-final.log` |
| `docker compose up -d --wait` | 0 | 独立主服务启动；API/DB healthy，worker运行且真实心跳健康 |
| `uv run python scripts/prepare-e2e.py`，专用Compose `up -d --wait` | 0 | 独立双源测试环境就绪 |
| `uv run python scripts/verify-docker.py` | 0 | 真实HTTP/PG/worker/API密钥/批量→重启→备份恢复全链路 |
| `docker image save --output dist/muyaju-data-service-1.0.0-linux-amd64.tar muyaju-data-service:1.0.0 postgres:17-bookworm` | 0 | 两镜像离线包220,028,416字节，约209.8MiB |
| `docker image load --input dist/muyaju-data-service-1.0.0-linux-amd64.tar` | 0 | 两镜像实际重新载入成功 |
| `uv run python scripts/record-delivery.py` | 0 | 主应用空态、鉴权、无看板、心跳、镜像非root/无秘密、源码和包SHA核验 |
| `uvx pip-audit -r requirements.lock --format json -o evidence/python-dependency-audit.json` | 0 | 22项Python运行依赖未检出已知漏洞；不覆盖基础镜像OS |
| `uv run python scripts/package-source.py` | 0 | 独立源码ZIP完整性通过，排除已知本地凭据、运行配置和数据库；包含部署文件、文档与测试 |

开发与独立最终测试合计290项通过。独立测试还通过真实TCP Uvicorn链路、命名游标响应限额、两个不同结构的数据集、并发发布时同快照批量、真实worker子进程中断恢复。完整记录见[独立报告](independent-test-report.md)。

交付复核：独立代理实际检查镜像内16个Python/SQL文件摘要与最终测试候选一致，UID10001，无.env/.local/tests；离线包SHA与manifest一致。文档36个本地链接无缺失，两个数据集与两个来源配置模板经真实模型校验。源码包与镜像包分别交付，均有SHA256文件。

Docker E2E：供应商签名POST源和通用分页GET源，均为本地合成测试源；3天、2表、6个发布日期，共15条事实。商品支付合计240.000000、账单利润75.000000；库存两组7与3。精确带空格维度筛选及前导零ID保持。数据库/API/worker停启后整份批量查询结果一致，机器密钥仍可用，管理员重新登录成功。

备份恢复执行真实pg_dump/pg_restore到新库，8张关键表逐行稳定序列化SHA256相同，包含两事实表、定义、批次、发布、管理员、机器密钥、模式版本。辅助会话/限频/分片/心跳表没有逐表比对，不冒称全表校验。重复恢复同库实际退出2，拒绝覆盖，原应用连接未切换。证据为`evidence/docker-e2e.json`。

性能：独立4万行/100组导入约8.44秒，含tracemalloc开销；Python被跟踪分配峰997,378字节，**不是RSS或整机内存**。5并发，各范围15样本，1/30/180天查询p95约159.23/145.02/140.53毫秒。数据为1个非空日+179个空发布日，不能外推真实720万行、最坏字段宽度、生产SLO或最低硬件配置。

## 三、缺陷、风险与回滚

独立发现的P1金额边界：Python默认Decimal精度使abs判断误拒NUMERIC(38,6)合法极值，查询代理改为无损比较并补正负极值，独立复验通过。独立P2响应预算：宽字段与500行组合可能形成过大结果，查询代理改命名服务器游标逐行检查8MiB单结果、16MiB批量总JSON预算，真实大文本验证413和缩列可查，已关闭。

开发自检另修复：筛选意外改变明细投影列表、同事务队列时间相同时日期任务顺序随机、停止worker短期心跳被误判健康。对应测试保留并通过。曾有两类测试方法失败：真实HTTP到达时刻间隔不能等同客户端发起间隔，独立补严格受控单调时钟并保留真实整体窗口验证；压缩响应夹具构造阶段提前解压，改流式夹具。原失败证据保留，未删测试或降低核心断言。当前无已知未关闭P0/P1。

仍未验证：真实供应商实连及其刷新窗口、真实业务对账、720万行数据/长期磁盘增长、目标服务器/TLS/网关、异地灾备、CI、人工专项评审及生产发布。没有上一正式版本可执行旧代码回退；本轮验证的是同版本重启及恢复到新库，不声称完成生产降级。

服务采用每日快照，不是所有表的任意增量合并；无日期维表需上游提供明确快照日期。字段变更不自动迁移，需新数据集并验证切换。跨表批量是独立查询，非JOIN。跨多次HTTP offset分页可能遇到新发布，调用方需检查published_versions，不能当不可变导出游标。

回滚：常规停止保留数据库卷；失败日同步保留旧发布；恢复先新建库校验，不覆盖或自动切换应用。生产版本回退/换库须另行确认维护窗口、恢复点和补数安排。已保留新项目与测试资源，未删除旧项目任何内容。

## 四、质量状态

| 状态项 | 结果 |
| --- | --- |
| Codex开发完成 | 独立多数据集服务本地版完成 |
| 测试计划/用例已生成 | 是，R01-R10追踪及独立矩阵 |
| 自动化测试已执行 | 是，最终290项通过及Docker/恢复链路 |
| 人工验证已执行 | 否，Codex自动验证不等于人工验证 |
| 剩余未验证项 | 真实源、业务对账、真实规模、目标服务器和异地灾备 |
| 独立复核通过 | 本地候选有条件通过，缺陷已复验关闭 |
| CI通过 | 未执行 |
| 开发最终确认 | 待人工确认 |
| 人工代码评审通过 | 未执行 |
| 允许发布 | 独立本地服务已运行；生产发布未批准 |

**AI自检结论：有条件通过。Codex自检通过不等于开发自测确认、人工代码评审、CI验证或发布审批通过。** 下一门禁是配置真实来源后的联调和业务对账、人工专项评审、目标Docker服务器环境验收。
