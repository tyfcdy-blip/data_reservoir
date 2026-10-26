# 云部署候选版本验证记录

## 范围与基线

- 模式：全过程开发；本地子路径适配为 L2，云端数据库迁移和正式切换为 L3，分阶段验收。
- Git 基线：main，2c69620187f3518f70ec9a76e2ae49bcfeb7e542；开始时工作区干净。
- 开发位置：基线文件的独立副本，不连接原运行数据库，不启动同步，不操作经营看板。
- 本阶段修改路径配置、管理后台 URL、部署模板和测试；不更改业务口径、权限模型、数据库结构。
- 数据库迁移、云端老内核运行、共享 Nginx 配置及实际切换尚未执行；必须拿到验证证据后逐步推进。

## 工作包及用例

|工作包|实现|用例及预期|层级|
|---|---|---|---|
|P1 后端路径|Settings/API|空前缀与 /data-service 均正常；非法前缀拒绝；首页和文档链接保留前缀；OpenAPI server 正确|单元/API|
|P2 前端路径|static|资源、登录、API、复制地址均使用所在服务前缀；会话按路径隔离；根路径兼容|JS/浏览器|
|P3 云配置|deploy|独立 Compose；数据库无宿主机端口；worker 默认不启动；Nginx 剥离指定前缀；卷持久化|配置/隔离集成|
|P4 回归复核|tests|匿名仍拒绝；路径不影响既有路由；恢复和回滚步骤无覆盖旧数据；独立检查证据|回归/独立审查|

## 门禁与回滚

隔离测试使用可识别的新测试数据。不能将源接口或生产数据请求作为本阶段测试。回滚本地候选文件不影响运行环境；云端切换前保留原镜像标识、Nginx 配置、数据库备份及原加密密钥。新云数据库若有写入，不得直接回到旧快照造成数据丢失，应先停写并分析差异。只允许一套同步 worker。发布、数据库迁移和新提交推送需给出可审阅结果后确认。

## 实际结果

2026-10-26 本地隔离验证完成，未进行云端部署。原运行项目与数据库没有纳入测试；测试 PostgreSQL 使用独立容器与临时存储。

|命令/证据|结果|
|---|---|
|`python -m pytest tests/test_api.py tests/test_deployment_prefix.py tests/test_deployment_prefix_api.py tests/test_ddl_parser.py tests/test_formula_models.py tests/test_schedule_times.py tests/test_package_source.py tests/test_repository_bootstrap.py deploy/cloud/test_deployment.py -q -p no:cacheprovider --basetemp=<独立可写目录>`|退出0，283通过，0失败/跳过；包含真实测试库登录、导入、金额查询、密钥撤销及根路径回归|
|`node tests/admin_browser/subpath-config.cjs`|退出0，24组检查、45次隔离API请求；使用真实Edge，认证与数据边界打桩|
|`node tests/admin_browser/formula-config.cjs`|退出0，17项前端公式配置回归|
|`python deploy/cloud/verify-isolated-transfer.py`|退出0，5项真实PostgreSQL备份恢复检查；拒绝覆盖备份和已有库|
|修改Python文件及 `deploy/cloud` 的 `ruff check --no-cache`|退出0，无错误|
|3个修改JS的 `node --check`；恢复脚本 `bash -n`|均退出0|
|`docker build --pull=false -t muyaju-data-service:cloud-subpath-test-20261026 .`|退出0，新测试镜像，未覆盖运行镜像|
|独立 Nginx 1.28 + 新应用镜像 + PG17 的本机联调|`nginx -t`及16项代理、资源、真实认证检查通过；临时网络和容器已清理|

本机Python 3.12，测试依赖来自现有服务虚拟环境；Windows测试与Linux容器验证同时执行。测试框架提示已有 httpx TestClient 将弃用，未为本次路径适配升级核心依赖。完整本地输出保留在本任务工作区的 cloud-prefix-final-results.txt、cloud-prefix-independent-review.md 等证据文件中，不携带生产数据。

## 缺陷与复验

- 初轮只给FastAPI设置root_path时，代理剥离前缀后的静态挂载出现404。改为由Uvicorn的 `--root-path` 生成完整ASGI路径，Compose固定启动参数与DS_ROOT_PATH一致；真实Uvicorn及Nginx联调均通过。
- 首次从纯源码运行旧测试时缺少被Git排除的运行配置文件。仅在隔离副本运行原初始化脚本补齐测试空配置，249项相关回归通过；没有从生产复制配置或凭据。
- 新增中文模板注释使原bootstrap用例默认GBK读取失败，明确以UTF-8读取后复验通过。新增测试夹具导入的Ruff冲突改为pytest插件注册，最终静态检查及283用例通过。
- 独立复核发现P2两项：源码包未包含deploy模板、云运行配置可能误提交。增加六个部署文件显式白名单、运行配置忽略规则及云凭据内存检查；独立复跑打包27项和Git忽略11项验证关闭。
- 无网络镜像构建因缓存缺失失败，恢复标准构建网络模式后依赖层命中缓存并构建成功，未变更依赖锁文件。

## 质量状态与下一门禁

|项目|状态|
|---|---|
|Codex本地开发完成|是，子路径适配和部署准备范围|
|测试计划/用例生成|是|
|自动化测试执行|是，范围和模拟边界如上|
|人工验证|未执行|
|独立代理复核|本地准备有条件通过，两项P2关闭；未发现本范围未关闭P0/P1|
|CI|未执行|
|开发人员最终确认|待用户确认|
|人工代码评审|未执行|
|允许正式发布|否，云端门禁尚未完成|

AI自检结论：本地准备有条件通过。尚未验证阿里云旧内核、真实HTTPS/server配置、最终数据迁移/加密配置对账、生产负载和实际磁盘占用。下阶段先只读核对云配置，再确定新镜像版本与提交清单、恢复演练、切换窗口；不提前停止本机同步。Codex自检通过不等于开发自测确认、人工代码评审、CI验证或发布审批通过。
