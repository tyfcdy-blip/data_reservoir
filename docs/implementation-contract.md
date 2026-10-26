# 独立多数据集服务实施契约

> 历史说明：本文引用的 evidence 文件、截图和日志仅本机保管，不随 Git 仓库分发；历史测试结果不代表本次或云端验证。

2026-10-22；全过程开发；L3。用户纠正：只交付独立数据服务，支持配置多张表，不含经营看板或其前端依赖。新项目与数据库、卷、端口完全独立，不迁移旧应用数据。无生产/真实源调用、镜像推送或Git操作。

## 关键路径与工作包

M1 契约和测试计划 → M2 类型化数据集/采集/鉴权 → M3 Docker及真实HTTP集成 → M4 独立测试、缺陷交实现者修复复验 → M5 本地交付。

| 包 | 写入者 | 独占范围 | 验收 |
| --- | --- | --- | --- |
| WP1 | 主代理 | config/db/schema/auth/api/cli、Docker、脚本、总体文档 | 新项目独立启动，管理身份与机器密钥隔离、无看板依赖 |
| WP2 | 查询代理 | models.py、datasets.py、query.py、tests/test_datasets.py、tests/test_query.py | 多表类型注册、参数化明细/聚合/批量一致性、安全/边界 |
| WP3 | 接入代理 | ingest.py、source.py、worker.py、tests/test_ingest.py、tests/test_source.py | HTTP通用及供应商协议、分页、幂等分批写入、原子发布、恢复/保留 |
| WP4 | 独立代理 | tests/independent/、独立报告、evidence/independent/ | 实现者之外独立检查、真实PG、故障、安全、4万行专项、缺陷复验 |

## 共享接口（修改先协调）

Python包data_service；FastAPI+psycopg3+PostgreSQL17；Python3.12。配置前缀DS_，Settings.database_url/admin_username/admin_password/session_hours/statement_timeout_ms/max_request_bytes/source_config_path/allowed_source_hosts/schedule_enabled/schedule_hour/refresh_days。DB connect(settings)返回dict_row连接，设置statement_timeout与lock_timeout。ServiceError(status,code,message,details=None)在errors.py，不包含敏感原文。时间Asia/Shanghai，业务日期只接受YYYY-MM-DD，保留180天（含今天，但不接受未来）。

数据集DatasetDefinition：slug(小写字母开始，字母数字下划线，≤48)、name(≤120)、date_field、key_fields(1..5不重复，字段存在且required)、fields(1..80, FieldDefinition{name,type:string/integer/decimal/date/datetime/boolean,required=false,dimension=false,max_length=255})、retention_days(1..180默认180)。date_field类型date且required；字段及别名允许英文字母开头的字母数字下划线，最长63字符（可含大写）；不得以下划线开始、重复或使用内部保留名。字段定义创建后不可改，重复完全相同注册幂等，差异409；禁止自动破坏性迁移。

datasets表: slug TEXT PK, name TEXT, table_name TEXT UNIQUE, definition JSONB, created_at timestamptz。每数据集独立类型化records_<uuidhex>表；内部列_batch_id UUID FK batches(id) ON DELETE CASCADE,_date DATE,_key TEXT；PK(_batch_id,_key)，日期/批次索引；其余用户列使用安全Identifier，不拼接未校验文本。金额NUMERIC(38,6)、整数BIGINT、日期DATE、时间TIMESTAMPTZ、文本VARCHAR(max_length)、布尔BOOLEAN。无SQL透传、eval或任意表达式。

batches表: id UUID PK,dataset_slug FK datasets(slug),business_date DATE,mode(pull/push),status(pending/running/uploading/succeeded/failed/cancelled),owner_key_id UUID nullable,expected_rows BIGINT nullable,row_count BIGINT default0,page_count INTEGER default0,idempotency_key TEXT nullable,created_at/updated_at timestamptz默认now,started_at/finished_at nullable,error_code/error_message nullable。UNIQUE(dataset_slug,idempotency_key)；每dataset/date仅一active(pending/running/uploading)。batch_chunks(batch_id FK cascade,chunk_index INT,digest TEXT,row_count INT,PK(batch_id,chunk_index))；published_days(dataset_slug,business_date,batch_id FK batches, row_count,published_at,PK(dataset_slug,business_date))。worker_heartbeat(worker_key PK, heartbeat_at,status)。

WP2 datasets.register_dataset(settings,definition)->dict；get_dataset(connection,slug)->dict（包含definition和table_name）；list_datasets(settings)->list；validate_record(definition:DatasetDefinition,row:dict,business_date:date)->dict类型化值（未知字段/非法数值拒绝，允许源映射主动丢弃已配置外字段）；query.execute_query(connection,slug,payload:QueryRequest)->dict，query.run_query(settings,slug,payload)->dict；query.run_batch(settings,requests:list[BatchQueryItem])->list。业务输出Decimal转十进制字符串、date/datetime ISO，NULL保留，不补0。

QueryRequest:start_date,end_date,fields(list可选),filters(list Filter{field,op:eq/in/gt/gte/lt/lte/is_null,value}),group_by(list),aggregates(list Aggregate{field可选,op:sum/avg/min/max/count/count_distinct,alias}),order_by(list Order{field,direction:asc/desc}),limit(1..500默认100),offset(0..100000默认0),require_complete默认true。明细与聚合模式互斥；分组字段须dimension=true（日期可分组），sum/avg只数值；列/操作/别名白名单，拒绝NULL比较歧义/空in等。最多20filter、200个in值、10group、20aggregate。未设置排序必须按全主键/分组维度稳定排序。返回dataset,columns,rows,total,limit,offset,coverage{complete,missing_dates},published_versions。范围含两端，最长180天且在dataset保留窗内；缺日默认409，可require_complete=false显式查询已发布部分。批量≤10跨dataset子查询在同一REPEATABLE READ READ ONLY快照中执行；任一失败整体失败，无部分成功。

WP3 ingest.create_batch(settings,slug,business_date,expected_rows,idempotency_key,owner_key_id=None,mode='push')->dict；append_chunk(settings,batch_id,chunk_index,rows,owner_key_id=None)->dict；commit_batch(settings,batch_id,owner_key_id=None)->dict；cancel_batch同参数；get_batch(settings,batch_id)->dict；enqueue_sync(settings,slug,start_date,end_date)->list；操作必须在自己的事务中锁batch；owner_key_id不匹配403（管理/worker用None允许）；追加最多1000条、期望最多1000000行；chunk重发摘要相同幂等，不同409；跨chunk重复键整批失败或拒绝且不能发布；commit检查expected_rows及chunk连续，从未成功前无数据可读。publish指针、清旧批事实与状态同事务，失败留旧。已成功重提commit幂等。取消只删除本批未发布暂存。查询不会读取暂存。

HTTP源配置文件config/sources.json，按dataset_slug配置，凭据仅env引用；通用http_json支持GET/POST、日期/页码/页大小参数名及query/json位置、列表点路径、静态参数、目标字段→源字段映射、header_env{name:ENV_NAME}；vendor_batch协议兼容已知batchQueryData签名。源URL只由部署配置设定，hostname必须在DS_ALLOWED_SOURCE_HOSTS显式白名单，禁重定向/URL凭据/URL查询片段、有限超时/重试/响应体/页数/限速。通用协议明确page_start、page_size并取到空页，字段映射后完整校验。不得通过数据调用请求任意URL。source模型/详细字段由WP3定义并及时告知主代理。源/调度仅拉昨日及以前，每日调度默认关闭；API手工范围最多180天。worker全局单实例advisory lock，重启将running置failed清暂存，保留旧发布，pending继续；隔离每dataset/day，一个失败不阻断其他日期。过期uploading清理默认24h；保留按dataset天数，删除过期日及旧任务。

开放查询：POST /api/v1/datasets/{slug}/query、POST /api/v1/query/batch、GET /api/v1/datasets（授权子集）、GET /api/v1/datasets/{slug}。X-API-Key，密钥按dataset授权read/write；write仅批次导入，不能配置源/建表/同步/管理。管理Bearer会话与机器key完全隔离；管理员可调用数据API以调试。密钥只返回一次，DB存hash、到期和撤销、每key默认60次/分钟。管理POST /api/v1/auth/login、logout；POST /api/v1/admin/datasets；POST/GET /api/v1/admin/keys；DELETE /api/v1/admin/keys/{id}；POST /api/v1/admin/datasets/{slug}/sync；GET /api/v1/admin/status；导入POST /api/v1/datasets/{slug}/batches，PUT /api/v1/batches/{id}/chunks/{index}，POST commit/cancel，GET batch。不提供RuoYi getRouters/getInfo/dashboard接口。

服务公开localhost8090；容器api+worker+init+db，无web/Node/相邻目录build依赖；新DB和volume，不连接旧应用库。OpenAPI供其他项目接入，/docs使用受信静态Swagger或静态本地接口指南，不强制外网CDN。本轮真实源、生产环境、完整720万行未验。

## 测试计划与门禁（编码前）

R01独立部署：单目录镜像build/up、匿名health、无前端目录引用；R02注册两张完全不同schema/重复幂等/不兼容拒绝；R03主键前导零/空/超长/日期/Decimal精度/null/注入；R04推送分页重试/重复/部分失败/原子覆盖/取消/权限；R05HTTP参数/映射/分页/超时/限频/错误/断点失败保旧；R06范围/多维/聚合/排序/分页/批量一致性/缺日；R07机器key跨表越权/只读写入拒绝/撤销/过期/限频/日志脱敏；R08worker重启/180天/并发；R09Docker重启/备份恢复/离线镜像；R10独立测试与缺陷复验。

测试仅使用新专用*_test库、合成多数据集和本地HTTP假源，不能使用真实源或旧应用数据。每WP静态+针对性PG/API测试；最终完整回归、真实HTTP容器链路、独立测试。无P0/P1，实际证据保存evidence；未验证不写通过；人工/CI/生产审批独立列出。


## 交付状态

M1-M5本地范围完成，用户纠正的独立多数据集项目已实现。文档代理补充接口与运维两文件和两份配置模板；独立67项已通过，开发223项通过，真实Docker链路和恢复通过。详见delivery-report.md，不把本地有条件通过当作真实源/CI/人工/生产验收。

## 1.2.0追加契约

QueryRequest与QueryTemplate增加formulas（最多10项，alias/expression/decimal_places，默认空）及result_filters（最多20项，默认空）。不改历史字段和响应，详细安全运算、精度、边界和计算顺序以[API契约](api-contract.md)为准。本次实现、测试、回滚与缺陷闭环见evidence/formula-plan.json及formula-delivery.json。
