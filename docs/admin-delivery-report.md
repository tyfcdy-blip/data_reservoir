# 管理配置后台交付验收记录

> 历史说明：本文引用的 evidence 文件、截图和日志仅本机保管，不随 Git 仓库分发；历史测试结果不代表本次或云端验证。

日期：2026-10-22；项目：`D:\codex\慕雅居数据服务`；应用版本1.1.0，数据库版本2。当前入口：`http://127.0.0.1:8090/admin/`。本次只构建独立数据服务的管理员后台，未修改经营看板项目。

## 一、任务闭环

模式为全过程开发；按L3执行，原因是持久化配置、来源凭据、公开查询规则和数据库升级。用户已授权独立项目、本机运行、Docker交付及子代理协作；本轮没有真实第三方请求、生产部署、账号修改或Git提交。

该目录不是Git仓库，没有分支、commit或Git差异可报告。开工时读取真实代码、配置及测试，备份旧库到 `.local/backups/pre-admin-v2.dump`；源码22文件基线与哈希存于 `.local/baseline-admin/`。基线12文件变更，运行时新增13文件，清单见 `evidence/admin-changes.json`。这项统计仅覆盖开工捕获的运行时/构建基线，不冒充文档、测试全部改动的Git统计。

|需求/阶段|工作包与实现|用例与结果|状态|
|---|---|---|---|
|登录与配置后台/M3|前端代理：static下8文件；主代理：api静态路由、响应安全头|主浏览器11检查、独立浏览器10检查、本地8090登录与六页冒烟|完成|
|来源→目标表/M1|来源代理：source_registry/source_routes；SourceClient凭据隔离、映射与单页测试|新增42来源用例、独立管理API与真实HTTP用例|完成|
|同步运行/M1|来源代理：worker任务间加载、每源调度、禁用；ingest入队统一加载|同一worker进程修改URL/凭据后下次任务取新数据、禁用与调度幂等|完成|
|加工与开放/M2|主代理：query_services/query_service_routes；复用既有query引擎|20条配置/聚合/权限/并发用例及独立精度/规则覆盖测试|完成|
|数据库与配置/M1|主代理：migration_2.sql、db迁移、Settings、配置脚本、Fernet依赖|v1已发布行和版本升级保留、重复初始化、高版本拒绝降级|完成|
|交付/M4|主代理：Docker1.1.0、文档/镜像包/源码包；独立代理：验收|容器真实HTTP、多表查询、命名规则/密文重启持久化、恢复哈希一致|完成|

后台六页为服务概览、数据表、接口接入、加工与开放、调用密钥、同步记录。来源可配置HTTP参数、分页、字段映射、凭据及每日回刷；加工可配置明细选列、分组、sum/avg/min/max/count/count_distinct、固定筛选与排序。保存后提供命名查询接口。其他系统传日期范围、分页及白名单筛选；原逐表和多表批量API兼容。

现有字段定义保持不可变，名称和类型变更需要新表标识。来源凭据认证加密保存，密钥来自部署环境，API不回显凭据；机器密钥只在创建时显示一次。没有引入任意SQL、脚本或未经确认的经营指标计算。read权限依然是整表读取权限，固定模板不是行/字段权限隔离。

## 二、测试证据

测试环境：Windows、Python3.12项目虚拟环境、PostgreSQL17、Docker Linux容器、Microsoft Edge/Playwright。所有业务写入仅发生于专用测试库/专用容器项目；HTTP源为本机合成服务。实际主库无数据集、无业务数据、无来源配置、无命名服务，自动来源调度未开启。

测试计划与矩阵见 `admin-console-plan.md`；独立报告见 `admin-independent-test-report.md`。核心查询/导入使用真实数据库和实现；部分来源异常测试仅Mock HTTP边界，真实HTTP/worker和浏览器链路另外执行，未将业务核心替换为Mock。

以下命令均在项目根目录执行，最终退出码均0：

|命令|最终通过/失败/跳过|证据|
|---|---|---|
|`uv run --env-file .local/test-admin-api.env pytest tests/test_query_services.py -q --junitxml=evidence/admin-query-services.xml`|20 / 0 / 0|admin-query-services.xml|
|`uv run --env-file .local/test-admin-sources.env pytest tests/test_admin_sources.py -q --junitxml=evidence/admin-sources.xml`|42 / 0 / 0|admin-sources.xml|
|`uv run --env-file .local/test-admin-independent.env pytest tests/admin_independent -q --tb=short --junitxml=evidence/admin-independent/api-runtime-final.xml`|43 / 0 / 0|admin-independent/api-runtime-final.xml|
|`uv run --env-file .local/test-api.env pytest tests/test_api.py -q --junitxml=evidence/admin-regression-api.xml`|23 / 0 / 0|admin-regression-api.xml|
|`uv run --env-file .local/test-query.env pytest tests/test_datasets.py tests/test_query.py -q --junitxml=evidence/admin-regression-query.xml`|107 / 0 / 0|admin-regression-query.xml|
|`uv run --env-file .local/test-ingest.env pytest tests/test_ingest.py tests/test_source.py -q --junitxml=evidence/admin-regression-ingest.xml`|93 / 0 / 0|admin-regression-ingest.xml|
|`uv run --env-file .local/test-independent.env pytest tests/independent -q --junitxml=evidence/admin-regression-independent.xml`|67 / 0 / 0|admin-regression-independent.xml|
|`uv run ruff check data_service scripts tests`|通过|命令退出0|
|`uv run python -m compileall -q data_service scripts tests`|通过|命令退出0|
|`node tests/admin_browser/flow.cjs`|11检查通过、0脚本错误|admin-browser/result.json、桌面与窄屏截图|
|`node tests/admin_independent/browser-admin.cjs`|10检查通过、0脚本错误|admin-independent/browser-result.json|
|`docker compose build api`|镜像构建通过|镜像ID见admin-delivery-manifest.json|
|`docker compose --env-file .local/e2e.env -f tests/e2e/compose.yml up -d --no-build --wait`，`uv run python scripts/verify-docker.py`|通过|admin-docker-e2e.json|
|`docker compose up -d --no-build --wait`，`uv run python scripts/record-delivery.py`|本机容器/API/worker健康、静态资源及真实登录通过|admin-delivery-manifest.json|

自动化Python用例合计 **395通过、0失败、0跳过**，其中旧用例290条、新增开发62条、独立新增43条；加强迁移2条是43条中的复验，不重复计入。浏览器21检查单列，不混入395条。既有TestClient提示httpx弃用，未为消除提示升级核心依赖。

主浏览器流程通过页面新建表、保存GET来源和加密Authorization、映射、测试3行、worker翻页发布、按shop求和得到130.000000及20.000000、开放接口、创建read密钥、外部筛选调用、撤销后401、退出会话撤销。页面表格内部允许滚动；1440桌面和390窄屏无文档整体横向溢出。独立浏览器另外验证XSS名称安全显示、401清理和移动导航。

Docker专项使用供应商签名POST及通用GET两个合成源，完成6个日期发布；多表汇总为支付240.000000、利润75.000000，仓库存量7和3。重启API/worker/数据库后结果、机器密钥、加密来源和命名查询规则保留。备份恢复到新库后10张相关表指纹一致；重复恢复同库被拒绝，退出2属于预期。旧v1备份另恢复到 `ds_restore_admin_pre_v1`，旧1.0.0镜像对该恢复库ready通过，未替换实际应用库；证据 `admin-upgrade-rollback.json`。

## 三、问题、风险与回退

|缺陷|等级|处理|
|---|---|---|
|命名查询校验器复用签名错误，保存/预览500|P1|主代理修正显式字段校验包装；原失败断言全部复验通过|
|来源凭据非法Unicode触发编码500|P2|来源代理拒绝非法Unicode、NUL、非有限值和非法HTTP头凭据；独立复验422|
|前端busy在无弹窗时访问空对象|P2|前端代理修复空值判断；主/独立浏览器复验零脚本异常|

无未关闭P0/P1。没有删除失败用例或降低断言。独立首轮失败证据保留，临时测试会话脱敏，最终报告明确实际复验。

未验证：真实供应商数据和金额对账、实际每日4万行及180天规模下的压测、长期日调度、长任务中途修改来源的故障注入、Safari/Firefox、远程主机网络/TLS与生产部署。并发修订保存由真实数据库竞争用例验证；不是生产吞吐容量承诺。后续接真实来源，应先用已结束的一天核对行数/金额，再开启定时和扩大历史补数。

数据库版本2不能让1.0.0直接降级使用。回退需停api/worker并保留卷，把升级前备份恢复到新命名库，核对数据后切回旧镜像/连接。密文备份需要匹配的 `DS_CONFIG_ENCRYPTION_KEY`，必须与数据库备份分别安全保存；不要随机替换现有加密密钥或删卷。操作细节见admin-guide.md。当前仅本机发布，不代表远程或生产发布获批。

## 四、质量状态及交付物

|状态项|本轮实际状态|
|---|---|
|Codex开发完成|是，六页及真实后端配置/查询已接通|
|测试计划/用例已生成|是，开发与独立矩阵、自动化及证据齐全|
|自动化测试已执行|是，395条Python及21个浏览器检查通过，Docker专项通过|
|人工验证已执行|否；浏览器和截图视觉检查由Codex自动化/AI完成|
|剩余未验证项|上述真实源、规模、长运行、远程环境等|
|独立复核通过|是，独立代理范围内代码/用例/结果复核完成|
|CI通过|未执行，无CI平台运行声明|
|开发最终确认|待用户/开发人员确认|
|人工代码评审通过|未执行|
|允许发布|已完成用户授权的本机Docker交付；不批准生产发布|

AI自检结论：**有条件通过**。已确认本地管理配置主链路、独立复核和回归证据；保留上述真实环境与人工门禁。下一门禁为用户按真实来源验证配置、业务对账及需要时的CI/人工评审。

交付：后台 `http://127.0.0.1:8090/admin/`；账号 `.local/admin-credentials.txt`；操作说明 `docs/admin-guide.md`。镜像包 `dist/muyaju-data-service-1.1.0-linux-amd64.tar`（225584128字节，约215.1MiB）及SHA256包含业务镜像和PostgreSQL；源码包 `dist/muyaju-data-service-1.1.0-source.zip` 包含前后端、配置模板、文档与测试，不含.env/.local/业务数据；包校验见 `evidence/source-package.json`。部署时使用新版本Compose，不要混用旧1.0.0包内文件。旧交付物保留供恢复参考。

**Codex自检通过不等于开发自测确认、人工代码评审、CI验证或发布审批通过。**
