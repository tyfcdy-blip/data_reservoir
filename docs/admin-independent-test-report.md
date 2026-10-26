# 管理配置后台独立验收报告

> 历史说明：本文引用的 evidence 文件、截图和日志仅本机保管，不随 Git 仓库分发；历史测试结果不代表本次或云端验证。

日期：2026-10-22。执行者：独立测试子代理。模式：独立审查与测试，不修改产品实现。等级：L3（配置持久化、来源凭据及开放查询权限）。结论：本报告范围内独立复核通过，发现的 1 项 P1、2 项 P2 均经其他代理修复并复验关闭。

## 边界

仅连接 `admin_independent_test` 专库；用例启动前校验数据库名。测试表以 `ind_`、`migration_` 标识。仅向本机合成 HTTP 来源发请求，不连接主库、不调用真实第三方。实现代码不由本代理修改。浏览器及集成测试均使用合成业务数据，凭据不记录进报告或测试输出。没有人工评审、CI 或生产发布授权的推定。

## 独立用例矩阵

|编号|需求|层级/关键输入|预期结果|实现位置|状态|
|---|---|---|---|---|---|
|AI01|AC01/03/06|API，未登录及仅机器密钥访问管理接口|401；不能保存/读取来源或加工配置|test_admin_api.py|通过，8个入口各验两类调用者|
|AI02|AC03|API+DB，保存来源凭据、读取、更新不传凭据|数据库认证加密；无原文/密文回显；更新保留旧凭据|test_admin_api.py|通过|
|AI03|AC03|API，缺凭据、错误映射、路径不一致、未确认主机、非法Unicode|拒绝；没有部分落库；异常输入422|test_admin_api.py|通过，含P2修复复验|
|AI04|AC03|API，同一旧 revision 再次更新|409；原配置不被覆盖|test_admin_api.py|通过；此用例为顺序过期写入，真正同时竞争由主代理专项覆盖|
|AI05|AC04|API+真实本机HTTP，来源测试/同步/禁用|准确字段转换；不误发布测试页；禁用拒绝新同步|test_source_runtime.py|通过|
|AI06|AC04|常驻worker，更新URL/凭据与同日调度|同一进程下一任务使用新配置；每源调度幂等|test_source_runtime.py|通过；正在运行的长任务中途更新未单独注入|
|AI07|AC05/06|API+DB，分组、固定筛选、SUM/去重、NULL、Decimal|精确结果9007199254740993.300003；固定筛选不被覆盖|test_admin_api.py|通过|
|AI08|AC06|API，服务关闭、无数据集read权限、修改分组/表达式/白名单外筛选|拒绝；只列出有权限已开放服务|test_admin_api.py|通过|
|AI09|AC05/06|API，分页上限、缺覆盖、非法模板/标识符|422/409，不能改变完整性约束，不能注入SQL|test_admin_api.py|通过|
|AI10|AC08|浏览器，新建表→配置来源→同步→加工→开放→消费|真实页面配置和运行；SUM精确20.000000；0 JS异常|browser-admin.cjs|通过，10个检查点|
|AI11|AC08|浏览器，XSS名称、401、390px导航展开收起|文本安全显示；会话失效清理；无页面整体横向溢出|browser-admin.cjs|通过，截图人工视觉判断由AI完成|
|AI12|AC09|真实数据库，v1→v2、重复初始化、高版本库|管理员/表结构/已发布行及版本保留；拒绝降级|test_migration.py|通过|

## 实际执行与缺陷

环境：Windows、项目自有 Python 3.12 虚拟环境、真实本机 PostgreSQL 17、FastAPI TestClient，以及 Microsoft Edge 的 Playwright 自动化。HTTP 合成来源只运行于 127.0.0.1。UI 服务 18992 在验证结束后已停止。不存在生产源调用。

|实际命令（项目根目录执行）|退出码|实际结果|证据|
|---|---|---|---|
|`uv run --env-file .local/test-admin-independent.env pytest tests/admin_independent -q --junitxml=evidence/admin-independent/api-initial.xml`|1|首轮18通过、19失败，均定位为命名查询校验器P1|api-initial.xml|
|`uv run --env-file .local/test-admin-independent.env pytest tests/admin_independent -q --tb=short --junitxml=evidence/admin-independent/api-runtime-final.xml`|0|最终43通过、0失败、0跳过，17.51秒|api-runtime-final.xml|
|`uv run --env-file .local/test-admin-independent.env pytest tests/admin_independent/test_migration.py -q --tb=short --junitxml=evidence/admin-independent/migration-final.xml`|0|加强已发布业务行保留断言后2通过；属于上述43项中的迁移用例|migration-final.xml|
|`uv run ruff check tests/admin_independent`|0|全部通过|最终命令结果|
|`uv run python -m tests.admin_independent.browser_server`，另进程执行 `node tests/admin_independent/browser-admin.cjs`|浏览器0|10项检查通过，0 JS异常；主动撤销会话后的401为预期网络响应|browser-result.json、admin-desktop.png、admin-mobile.png|

43项为37个API用例、2个迁移用例、4个真实HTTP/worker用例。第一次 API 测试尚未加入 worker 用例。所有失败断言保留，修复由主代理/来源代理/前端代理完成，没有降低断言或删除失败测试。测试库在每例开始时重建，最后保留的测试状态不影响业务库。旧版全量回归、Docker镜像构建与恢复演练由主代理单独记录，本报告不把其他代理结果冒充本人执行。

已知测试工具警告：现有 FastAPI/Starlette TestClient 使用 httpx 发出弃用提示；本次未为了去警告升级核心依赖。测试仍实际执行并返回上述结果。

|缺陷|优先级|复现与影响|修复/复验|
|---|---|---|---|
|ADM-IND-01|P1|命名查询保存与预览全返回500。复用已绑定DatasetDefinition类校验器，Pydantic把ValidationInfo误传为字段值|主代理改显式classmethod包装；原19项失败全部转为通过，已关闭|
|ADM-IND-02|P2|来源凭据含JSON转义未配对代理字符时加密前UTF-8编码异常，API500|来源代理补Unicode边界校验；`test_source_invalid_unicode_is_validation_error[secret]`变为422，已关闭|
|ADM-IND-03|P2|浏览器busy收尾在无dialog时读null.dataset，产生未捕获JS异常|前端代理补modal存在性判断；全链路复验0 JS异常，已关闭|

浏览器初次截图抓到移动侧栏动画中间帧；最终测试等待动画完成再截图，并验证侧栏收起和导航点击。一次新增XSS断言失败是测试脚本在同一hash地址未刷新导致旧列表，改为明确reload后重新读取真实接口；没有修改产品或降低XSS断言。失败初轮保留为 browser-initial.json，最终结果以 browser-result.json 为准。

## 代码独立复核与验证限度

只读复核了 v2 增量事务迁移、来源配置加密/主机批准/凭据不读取任意进程环境、worker按任务边界刷新、命名查询固定规则和参数化SQL复用、重复读快照、机器数据表权限及前端DOM转义/会话清理/CSP。没有新增待关闭P0/P1。

命名查询的固定筛选和字段集合是接口规则，不是数据行/字段级安全边界。具有某数据表read权限的机器密钥仍可调用原始通用查询接口读取该表，管理页面“调用说明”及密钥页面已明确说明，本浏览器用例验证了说明存在。

独立验收未执行：真实供应商端到端、用户实际规模压测、长期定时运行、Safari/Firefox、生产部署、人工代码评审与CI；未声称这些通过。长任务执行中途修改配置、同时并发保存竞争和Docker恢复属于主代理专项证据范围，本独立报告不凭推测给出结论。当前43项与10个浏览器检查证明本机配置/处理/开放主链路及上述安全边界，不能代替生产上线验证。

## 质量状态

|项目|状态|
|---|---|
|Codex开发完成|本代理不负责实施；所审范围实现可运行|
|独立测试计划/用例已生成|是|
|独立自动化已执行|是，43项API/运行时及10个浏览器检查通过|
|人工验证已执行|否；截图视觉检查为AI检查|
|剩余未验证项|见上述验证限度|
|独立复核通过|是，本报告范围内|
|CI通过|未执行|
|开发最终确认|待开发人员确认|
|人工代码评审通过|未执行|
|允许发布|不作生产发布批准；仅向主代理提交本地交付门禁证据|

AI自检结论：有条件通过。范围内关键功能和缺陷复验通过，无未关闭P0/P1；生产源、规模性能和人工/CI门禁仍未验证。

## 质量责任边界

Codex 独立测试不等于开发人员自测确认、人工代码评审、CI 验证或发布审批。未执行内容必须标记未验证，不能推测通过。
