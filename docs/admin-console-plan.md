# 管理配置后台实施与验收计划

2026-10-22；模式：全过程开发；等级：L3（持久化配置、凭据及开放接口）。目标是独立数据服务的管理后台，不修改经营看板。既有数据库、源码基线已备份到 `.local/backups/pre-admin-v2.dump` 和 `.local/baseline-admin/`，不含任何真实业务源调用授权。

## 范围与工作包

|阶段|工作包/负责人|写入范围|验收|
|---|---|---|---|
|M1 配置持久化|主代理：v2增量迁移、密钥配置；接入代理：持久化来源、动态加载|db/config/migrations；source_registry/source_routes/source/worker及ingest中的来源加载|v1保留数据升级、重复初始化、密钥不回显、修改免重启|
|M2 加工与开放|主代理：命名查询服务、固定规则、外部调用授权|query_services/query_service_routes/api/query|聚合复用现有SQL；调用者不能覆盖加工规则；数据集权限生效|
|M3 管理界面|前端代理：离线管理后台|data_service/static/|真实登录、表结构、字段映射、来源/调度、查询服务、密钥、同步日志可操作|
|M4 验收交付|独立测试代理+主代理集成|tests/admin_independent、报告；主代理部署/文档/打包|API、安全、浏览器全流程、旧功能回归、Docker健康、备份恢复证据|

## 共享契约（实现须一致）

统一响应 `{code:200,data:...}`，错误沿用现有结构。管理员传 Bearer；外部传 X-API-Key。无任意SQL、表达式、跨表JOIN。保留原接口。

### 来源管理

`GET /api/v1/admin/sources` 返回数组；`GET/PUT /api/v1/admin/sources/{dataset_slug}`；`POST /api/v1/admin/sources/{dataset_slug}/test` 请求 `{business_date:"YYYY-MM-DD"}`，仅明确点击测试时请求单页。

PUT 请求：`{title,config:SourceConfig,secrets:{环境引用名:新值},enabled:true,approve_host:true,schedule_enabled:false,schedule_hour:3,refresh_days:7,revision:0}`。revision新建为0，编辑为读取值；保存后递增，不匹配409。config.dataset_slug必须与路径一致。secrets省略项保留；不回显原文。响应含 dataset_slug/title/config/enabled/schedule_enabled/schedule_hour/refresh_days/revision/secret_names/created_at/updated_at，不含密文。config仍使用现有header_env/vendor_app_key_env/vendor_app_secret_env引用格式；新凭据加密保存在数据库。approve_host是管理员确认访问此URL的主机；只对已保存来源生效。启用来源并不自动开启定时任务。

主代理建立表 managed_sources：dataset_slug TEXT PK FK datasets；title TEXT；definition JSONB；encrypted_secrets TEXT NULL；approved_host TEXT；enabled BOOL；schedule_enabled BOOL；schedule_hour INT；refresh_days INT；revision INT；created_at/updated_at TIMESTAMPTZ。Settings新增config_encryption_key，可缺省以兼容原无凭据配置，但保存新凭据必须有效。使用cryptography.Fernet。接入代理建立 `source_registry.load_active_sources(settings)` 统一合并文件与数据库；文件原load_sources保持纯文件读取。数据库禁用项遮蔽同名文件源。来源运行时密钥不得写全局环境。API与worker都调用active loader；worker自动重载、保留未变客户端限速状态、单任务内使用同一版本。

接入路由工厂 `build_router(settings, administrator)`，管理员依赖由主API传入。不会在导入时建立连接。

### 加工与开放管理

`GET /api/v1/admin/query-services` 数组；`GET/PUT /api/v1/admin/query-services/{slug}`。

PUT：`{name,dataset_slug,definition:{fields:null,filters:[],group_by:[],aggregates:[],order_by:[],require_complete:true},allowed_filter_fields:[],max_limit:100,enabled:false,revision:0}`。definition遵循现有QueryRequest字段约束但不含日期、分页。空fields表示所有明细；聚合模式fields必须null。aliases是返回列名。响应加slug/revision/created_at/updated_at。

`POST /api/v1/admin/query-services/preview`：`{dataset_slug,definition,start_date,end_date,limit:20}` 返回原查询结构。

`GET /api/v1/services`：只返回已开放且当前调用方具有数据集read权限的服务公开信息。

`POST /api/v1/services/{slug}/query`：`{start_date,end_date,filters:[],limit:100,offset:0}`；只允许白名单运行时筛选，不能改变固定字段/分组/计算/排序/完整性规则，不能超过max_limit（省略limit采用不超过100的配置值）。查询复用现有引擎与数据集授权、限流、响应体限制。

主代理建立表 query_services：slug TEXT PK；name TEXT；dataset_slug TEXT FK datasets；definition JSONB；allowed_filter_fields JSONB；max_limit INT；enabled BOOL；revision INT；created_at/updated_at TIMESTAMPTZ。

### 界面约定

同镜像、同域、无CDN，路径 `/admin/`，原根JSON与/docs保留。原生HTML/CSS/ES模块，命名入口 index.html、app.js、styles.css。深蓝侧栏、浅灰内容区、紧凑表格，蓝色主操作。六页：概览、数据表、接口接入、加工与开放、调用密钥、同步记录。登录走现有API，sessionStorage只保存会话token，401失效，退出真实撤销。所有外部字符串转义或textContent，禁止任意HTML。具备加载/空/错误/提交中/校验状态；新建表字段可视化编辑；不可编辑已有表结构。管理员可从真实空状态完成全链路，不使用演示硬编码数据。查询结果和curl示例可复制。不要在产品页面显示开发实现说明。

## 测试追踪与门禁

AC01真实登录/失效/退出；AC02注册表/非法字段/重复；AC03接入配置加密、掩码、缺凭据、并发修改；AC04来源测试、分页与同步、禁用、变更热加载、调度幂等；AC05明细/分组聚合/日期范围/空值/Decimal与规则校验；AC06开放服务关闭/数据集越权/规则覆盖禁止/过滤白名单；AC07机器密钥创建一次展示/撤销；AC08浏览器新增表→来源→同步→聚合→发布→机器调用及桌面/窄屏；AC09v1迁移/重复初始化/备份恢复；AC10旧290例回归及Docker实际运行。

测试用独立命名数据库和本机合成HTTP源；不调用真实外部软件。独立测试由不同代理执行；主代理复核证据和修复复验。无法执行项明确标未验证，不伪造人工评审、CI或生产发布。已有v1归档保留，v2数据库不由旧程序降级；回退需使用独立库恢复备份并切回旧镜像，保留v2原库和配置密钥。启动本机Docker属于已授权本地版本交付。
