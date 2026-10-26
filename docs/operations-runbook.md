# 多表配置、Docker 部署与运维

版本 1.1.0，2026-10-22。页面配置流程、加密密钥、热更新与v1升级回退见[管理员配置说明](admin-guide.md)。以下文件配置方式仍兼容；主机白名单由部署配置或管理员页面显式批准，普通调用方不能修改。只适用于本目录的独立数据服务。它拥有自己的 PostgreSQL 数据库、数据卷、API 端口和 worker，不依赖或改动经营看板项目。协议见[接口说明](api-contract.md)；实际执行结果以[交付报告](delivery-report.md)和[独立测试报告](independent-test-report.md)为准，本文的操作说明不是生产部署成功声明。

## 本地启动和交付边界

运行需要 Docker Engine、支持 Compose 的 Docker 环境；本机使用 Linux 容器。首次联网构建需要获取锁定的 Python 依赖及基础镜像。`scripts/configure.py` 只使用 Python 3 标准库，用于生成随机本地密码；没有 Python 时也可以按 `.env.example` 手工配置，不使用空密码。

在项目根目录执行：

```powershell
python scripts/configure.py
docker compose up -d --build --wait
```

默认配置：

| 项目 | 值/含义 |
| --- | --- |
| Compose 项目名 | `muyaju-data-service` |
| 服务 | `db`、一次性 `init`、常驻 `api`、常驻 `worker` |
| 镜像 | `muyaju-data-service:1.1.0`、`postgres:17-bookworm` |
| 接口 | `http://127.0.0.1:8090`；本地目录 `/docs`，OpenAPI `/openapi.json` |
| 数据库 | 独立库 `data_service`，本机绑定 `127.0.0.1:15433`，容器内 `db:5432` |
| 数据卷 | `muyaju-data-service_data-service-db` |
| 账号文件 | `.local/admin-credentials.txt`，仅本机保管 |
| 数据集/源配置 | `config/datasets.json` 根为数组；`config/sources.json` 根为 `{"sources":[]}` |
| 默认内容 | 两配置为空、每日调度关闭，无业务记录，不调用真实源 |

配置脚本发现已有 `.env` 时保留原配置，不覆盖账号或数据库密码。init 首次创建模式和管理员，并逐个注册配置中的数据集；再次运行不重置密码，也不自动修改已有数据集结构。多个数据集的配置注册不是跨表整体事务：前面的注册若已成功，后面的不兼容定义失败不会撤销前面成功项；修正后可幂等重跑。

检查服务：

```powershell
docker compose ps
docker compose logs --tail 100 api worker init
```

API `/health/ready` 检查数据库初始化；worker 没有 Docker HEALTHCHECK，要登录管理接口查看 `GET /api/v1/admin/status` 的心跳。`docker compose up --wait` 完成不能代替 worker 心跳和一次实际数据同步验收。

停止使用 `docker compose stop`，保留数据卷。**不要把 `docker compose down -v` 当作停止方式，它会删除数据。** 日志限制为每容器 3 个 10 MB 文件，不能代替长期审计归档。

## 离线镜像部署

交付归档路径为 `dist/muyaju-data-service-1.1.0-linux-amd64.tar`，目标是 Linux amd64，包含业务和 PostgreSQL 两个镜像。配套 `.sha256` 和交付记录用于核对文件。归档只包含镜像，不包含真实密码、机器密钥、数据库业务数据或原看板。

除了镜像归档，目标机器仍需要本项目的 `docker-compose.yml`、`.env.example`、`config` 和辅助脚本。可以复制完整项目源码交付目录，但**不要把开发机器的 `.env` 和 `.local` 一起分发**；在目标部署环境初始化独立配置。

```powershell
docker load --input dist/muyaju-data-service-1.1.0-linux-amd64.tar
python scripts/configure.py
docker compose up -d --no-build --wait
```

本机默认只绑定 127.0.0.1。如需提供给其他系统访问，由部署方设置 `DS_BIND_ADDRESS` 和访问网关，使用 TLS、网络访问控制和独立机器密钥；不应直接把 PostgreSQL 端口开放到公网。目标服务器的域名、证书、网关超时、网络策略和 CPU 架构必须独立验收，本地通过不表示这些已配置。

## 先定义数据集，再选择接入方式

[datasets.example.json](../config/datasets.example.json) 含两项：现有规范商品日表 22 字段子集、假定库存日快照 8 字段模板。它们不会自动注册，也没有附带业务记录。库存字段和接口均是说明异构表能力的假定模板，接真实库存时必须按实际结构重新定义。

商品样例使用 `dt`、`product_id` 作为日粒度身份，保留 `daily_id`，包含渠道/店铺/类目/负责人和支付/净销/成本/账单利润/支付预估利润等已知源字段。它不是完整 50 字段导入模板。需要更多字段时，应在**首次注册前**增补数据集及对应源映射；字段长度也是部署约束，样例的 120/500/1000 字符并非保证上游所有值都小于它。应先按源资料和合法样本确定，超长数据会明确失败而非截断。

把选定定义保存至 `config/datasets.json`，然后：

```powershell
docker compose run --rm init
```

也可使用管理员 `POST /api/v1/admin/datasets` 每次登记一个定义。API 注册只写数据库，不会自动回写 `datasets.json`，应另行纳入配置记录。不要直接把库存模板当作真实表、不要将测试业务行写入正式数据集。

同一 slug 的字段、主键、名称及保留策略注册后不可隐式改动，不一致返回 409。变更流程是新建版本化 slug、核对字段、重新导入、校验数据、更新调用系统授权与查询，保留原数据集到明确迁移完成；本版没有自动改表或删除数据集 API。

两种接入方式可以按数据集选择：

1. **拉取**：数据源由服务主动通过 HTTP 按单日分页获取；管理员只能为已配置源入队，普通调用方不能传任意 URL。
2. **推送**：其他系统先创建某日批次，分片上传，校验 `expected_rows` 后 commit；用逐表 `write` 密钥，不需要给它管理员密码或数据库连接。具体流程见[分批导入](api-contract.md)。

分批推送保持按日期完整替换；HTTP拉取遵循管理员保存的write_mode：replace_day整日替换，upsert在同一日期内按业务主键合并保留旧未返回行。查询只读完整发布版本。同一数据集同日禁止同时执行两个活动批次。长期未完成上传 24 小时过期，worker 定期清理；同日失败应检查原因后重跑，不手工修改发布指针。

## 配置多个 HTTP 源

[sources.example.json](../config/sources.example.json) 使用 `example.invalid` 占位域名，**不能直接调用**。根结构必须是 `{"sources":[...源对象...]}`；最多 100 个源，一个数据集只能配置一个源。每个源必须与已登记数据集对应，不能只改 URL 就假定字段结构兼容。

| 参数 | 默认与限制 |
| --- | --- |
| `dataset_slug` | 必填，目标已注册数据集 |
| `protocol` | `http_json` 或 `vendor_batch`，默认前者 |
| `url` | 必填，HTTP/HTTPS 端点；不得在 URL 中放账号密码、查询串或 `#` 片段；主机须部署白名单 |
| `method` | GET/POST，默认 POST |
| `parameter_location` | query/json，默认 json；GET 必须 query；POST 可二选一 |
| `date_param` | 默认 dt；发送格式始终为 YYYY-MM-DD |
| `page_param`,`page_size_param` | 默认 pageIndex/pageSize；三个参数名必须唯一 |
| `page_start` | 0 或 1，默认 0；供应商协议同样可配置，必须按真实接口分页语义选择。BDsDc38M0C应填1，0和1均返回其首页 |
| `page_size` | 默认 200，通用 1～1000，供应商 1～200 |
| `rows_path` | 默认 data；通用协议响应列表点路径，根数组可写 `$` |
| `static_params` | 默认空；最多 50 项标量参数，不能覆盖日期/页码/页大小，不能放密钥或 token |
| `field_mapping` | 默认空；`目标字段:源记录路径`，最多 80 项；非空时必须覆盖数据集**全部字段**且无额外目标字段 |
| `date_fields_from_datetime` | 默认空；需要从日期时间取自然日的**目标** date 字段列表，不能指向 datetime 或未登记字段 |
| `source_timezone` | 默认 Asia/Shanghai，必须为合法 IANA 时区 |
| `header_env` | 默认空；`请求头名:环境变量名`，最多 20 项，不把密钥明文放 JSON |
| `success_field`,`success_value` | 通用成功标记路径和值，必须一起配置；按类型和值精确比较，例如 true 与 1 不等同 |
| `vendor_api_id` | vendor_batch 必填；商品日现有接口 ID 为 B08GhuIzQB |
| `vendor_app_key_env`,`vendor_app_secret_env` | vendor_batch 必填，值为环境变量名字 |
| `timeout_seconds` | 默认 30，1～120；连接超时最多 5 秒；响应读取受限 |
| `max_retries` | 默认 2，0～5，表示失败后的重试次数；总尝试为 1 + max_retries |
| `max_pages` | 默认 10000，1～100000；达到上限仍未见结束空页则失败，不发布截断数据 |
| `max_response_bytes` | 单页默认 2 MiB，可设 1 KiB～16 MiB |
| `requests_per_second` | 每源默认 5，可设 0.1～20；同一客户端跨日期任务复用限频状态 |

字段点路径只按对象键逐级取值，不支持数组下标、通配符或表达式。没有配置映射时完整源记录直接进入校验，未知字段会失败；配置非空映射时仅保留显式选择列，未选择源字段丢弃，但缺任一已配置源字段仍整日失败，包括目标可空字段：源可以显式返回 null，不能静默漏字段。映射是选列/改名，不是计算公式。

### 主机与凭据配置

将实际源域名加入 `.env` 中的 JSON 数组，例如如下**占位**配置，接入时必须替换为实际合法域名：

```dotenv
DS_ALLOWED_SOURCE_HOSTS=["vendor.example.invalid","inventory.example.invalid"]
PRODUCT_SOURCE_APP_KEY=<部署环境中的应用标识>
PRODUCT_SOURCE_APP_SECRET=<部署环境中的应用密钥>
INVENTORY_SOURCE_AUTHORIZATION=<源要求的完整Authorization值>
```

这些示例不是有效凭据。环境变量通过 Compose 的 `env_file: .env` 传给容器；`header_env` 只读取命名环境变量，不自动添加 Bearer 前缀。如果源需要 `Authorization: Bearer ...`，对应环境变量就必须保存完整值。`DS_ALLOWED_SOURCE_HOSTS` 只写主机名，不写协议、路径或端口；客户端禁重定向、不使用系统代理环境、URL 内禁止凭据和查询串。Host 白名单由运维控制，不是可由普通 API 调用方扩大的任意抓取服务。

配置文件挂载只读。修改 `sources.json`、源相关环境变量或调度参数后，重建 API 和 worker 进程使二者读取同一配置：

```powershell
docker compose up -d --no-deps --force-recreate api worker
```

该命令会中断正在运行的同步，因此应先查看任务状态，在空闲时执行；如遇中断，新 worker 将原 running 任务标记失败并清未发布暂存，旧成功版本保持。管理员确认原因后重新入队。不要通过 `docker compose restart` 期待新 `.env` 值被加载；重建容器才应用环境变更。

### 通用 JSON：GET 或 POST

库存模板会按 GET 查询参数发送 `date=YYYY-MM-DD&page=1&size=200&include_inactive=true`，读取响应 `data.records`，检查 `success` 为布尔 true。响应记录形状示意：

```json
{
  "success":true,
  "data":{"records":[{
    "date":"2026-10-21","sku_code":"000123","warehouse_code":"示例仓A",
    "stock":{"available":8,"reserved":2},"cost":"12.340000",
    "observed_at":"2026-10-21T23:00:00+08:00","active":true
  }]}
}
```

若实际源为 POST JSON，设置 `method:"POST"`、`parameter_location:"json"`，同样的日期/页码/页大小和 static_params 会发送为 JSON 对象。若是 POST URL 参数则使用 `parameter_location:"query"`。本版不提供 form-urlencoded、XML、CSV、复杂嵌套请求体模板、游标分页或动态脚本适配；遇到不同协议需明确扩展并测试，不能猜测配置。

### 供应商商品日协议

`vendor_batch` 固定使用 POST JSON。商品日已知 API ID 为 `B08GhuIzQB`，`date_param:"dt"`，页码**从 0 起**，每页最多 200。对外请求结构由客户端构造：

```json
{
  "apiId":"B08GhuIzQB",
  "pageIndex":0,
  "pageSize":200,
  "batchQueryParams":[{"openFieldName":"dt","openFieldValue":"2026-10-21"}]
}
```

请求头由已配置环境变量计算 appKey、毫秒 timestamp 和签名；不在文档放真实值。签名只兼容供应商协议，传输保密仍依赖 HTTPS。该模式不允许 static_params，忽略通用 rows_path/success_field 的取数方式，固定要求响应 `success:true`、`code:"200"`、`data` 数组；额外顶层结构或非空 extraInfo 等未知形状会失败，不擅自猜测来源变化。

规范商品日源的 dt 是日期时间，本地需要 date：样例配置 `date_fields_from_datetime:["dt"]`。允许 YYYY-MM-DD 或明确格式的日期时间，带时区值先转 source_timezone 再取自然日，不带时区的来源值按 source_timezone 解释；转换后日期仍必须等于本次请求日期。该配置仅转换指定 date 字段，**不会给普通 datetime 字段自动补时区**。

源数字通过 Decimal 解析；目标 decimal 字段中的 NULL 保留，不能替换成 0。超过 NUMERIC(38,6) 范围、有效小数超过 6 位或主键重复将阻止当天发布。现有源部分字段为 DOUBLE，真实精度和字段长度是否符合本地约束需在首次真实对账确认，本地合成测试不能替代。

### 分页结束、限频与失败

必须一直取到空数组结束页；某一页不足 page_size 仍继续取下一页。源每页返回记录数不得超过配置 page_size，最多接收 100 万行/日。重复记录、漏日期、未知字段、无效日期/精度、超体积、未知响应结构、非成功标记等会使当天失败；不跳坏行、不发布部分合计。

429、500、502、503、504、网络错误和超时可以有限重试；等待使用有上限的退避，识别数字 Retry-After。其他 HTTP 错误或格式错误直接失败，重定向不跟随；源必须遵守 `Accept-Encoding: identity`，不接收压缩响应。多个来源配置各自限频：若不同源共享同一个供应商账户总额度，需要将各源 requests_per_second 合计控制在供应商额度内，本版没有跨源账户级配额器。

本地完成分页并不保证第三方在分页期间没有修改结果。建议在其日终刷新完成后同步，必要时重跑并核对源总数/金额；本版不能替不支持快照的上游制造一致性保证。

## 每日同步、保留和运行检查

默认 `DS_SCHEDULE_ENABLED=false`。首批接入时先保持关闭，登记数据集、配置源及环境变量、重新启动进程，管理员手工入队一个已结束日期，检查行数与金额后再扩大范围。

```dotenv
DS_SCHEDULE_ENABLED=true
DS_SCHEDULE_HOUR=3
DS_REFRESH_DAYS=7
```

此示例表示北京时间每日 03:00 之后，按日期回刷昨天至前 7 天，实际不超出各表保留窗口；03:00 不是已确认的真实源刷新完成时间，应按供应商实际情况调整。调度只拉昨天及以前，不拉今天。启动时如果已过指定小时，会检查当天应入队任务；文件来源仍按单小时调度。页面管理来源支持1～24个HH:mm时间；每个调度日、时点、业务日有独立幂等键，已失败时点不会无限重试，后续时点仍可同步。详见管理员配置使用说明。

worker 单实例独占数据库锁，不支持通过增加副本自动提高吞吐。一个日期同步失败会记录错误并继续处理其他任务；worker 连接断开时退出，由容器策略重启。重启先恢复中断状态，再处理 pending 任务。保留清理约每 5 分钟检查一次，各数据集独立事务；按北京时间当前日保留 `retention_days`，默认包含今天的最近 180 个自然日，过期发布日期和已结束任务会清理。保留清理是真实删除，不是归档；需要更长审计应另备份。

管理状态重点核对：

- `workers[].healthy`、心跳时间和容器运行状态；短时间 stopped 心跳仍可能新鲜，不能只看时间而不看状态。
- 每日任务 `status/error_code/error_message`；row_count为本次来源记录数，published_row_count为合并后发布总数；HTTP 202 只是排队。
- coverage 已发布日期数量；first_date 到 last_date 之间可能有缺口，用严格范围查询检查完整性。
- 数据卷可用磁盘、容器资源、备份新鲜度；本版没有内置短信/邮件告警或自动异地备份。

磁盘量由字段宽度、每天行数、索引、WAL、暂存新旧版本和备份共同决定。4 万行/日 × 180 日是 720 万行，仅这张表的行数；多表要分别估算。不要把小规模测试时延或 Python 跟踪内存峰值当作完整保留窗、PostgreSQL RSS 或硬件最低承诺。目标服务器配置应以实际全字段数据的存储与并发压测校准。

## 管理员密码和机器密钥轮换

管理员密码哈希存数据库。仅改 `.env` 然后普通 init 不会重置现有密码。需要重置时，管理员在安全位置修改 `DS_ADMIN_PASSWORD` 为至少 12 字符的新值，再执行：

```powershell
docker compose run --rm api python -m data_service.cli reset-password
```

操作更新管理员密码并撤销全部管理会话，不轮换其他系统的机器密钥，也不会自动更新 `.local/admin-credentials.txt`；保管记录需要同步更新。不要把密码直接放在终端命令行参数中。机器密钥通过创建新 key、调用系统切换、验证、撤销旧 key 完成轮换；已创建 key 的授权不能原地修改。不要把管理会话当作长期系统调用凭据。

`.env`、`.local`、测试证据和离线归档不进入业务镜像；Docker 构建上下文仍需保持正确 `.dockerignore`。不要上传含秘密的配置到仓库、共享文档或聊天，日志也不应开启 HTTP 客户端请求头调试。

## 备份、恢复与回滚

辅助备份脚本使用本机 Python 依赖环境；开发环境可先 `uv sync --frozen`。它通过 Docker 中的 PostgreSQL 工具读写二进制文件，不使用 PowerShell 文本重定向处理 dump。先确保 db 容器运行。

```powershell
uv run python scripts/database-snapshot.py backup
```

默认生成 `.local/backups/data-service-<UTC时间>.dump`，也可加 `--output <文件路径>`。已存在的目标文件会拒绝覆盖。备份非零退出或输出“备份失败”的文件不能用于恢复；保留供排查并重新生成有效备份。备份包含元数据、业务数据、管理员哈希、会话和机器密钥哈希，应按敏感文件保管、加密并异地复制。

恢复只创建新库，不覆盖原应用库：

```powershell
uv run python scripts/database-snapshot.py restore --input .local/backups/<备份文件>.dump --database ds_restore_check_20261022
```

库名必须为 `ds_restore_` 加 1～40 位小写字母、数字或下划线；已有同名库拒绝，返回 2。恢复失败返回 3，新库保留供排查；**不自动改应用连接、不删除旧库**。备份/恢复操作成功返回 0。脚本还支持 operation 前的 `--test-environment`，仅用于本项目专用隔离 E2E 容器，正式备份不要加它。

恢复后先在恢复库检查模式版本、已发布日、每表行数、关键金额、密钥/账号状态和查询结果。确认恢复点之后，需由运维明确批准停写切换，再同时修改容器 DS_DOCKER_DATABASE_URL 与本机 DS_DATABASE_URL 的数据库名，重建 API/worker/init 以统一连接。不要只切 API 而留 worker 写旧库，也不要未经核对就使用旧备份中的会话或密钥；如存在泄露事件，需要额外撤销和轮换。当前交付不代执行生产恢复或切换。

升级前保留上一镜像、配置副本及已验证可恢复备份；停下同步与外部写入，记录未结束批次。1.6.0 的数据库模式为版本 5，低版本程序不会自动回退较新模式。仅应用镜像变化且模式兼容时，可恢复上一镜像/配置并重启；出现结构不兼容则恢复至新库、对账后切换，不对在线库执行破坏性回退。同日重导会删除旧事实，不提供仅靠旧 batch_id 恢复历史版本的保证。

## 验证与故障定位

代码自检及测试命令、独立测试数量、Docker/恢复结果由[交付报告](delivery-report.md)记录。测试只能使用本项目明确隔离的 `*_test` 库与合成源，不能填写应用库、真实供应商地址或旧看板数据库。`scripts/verify-docker.py` 是隔离 Docker 链路验证入口；阅读脚本和报告了解它创建的专用资源，再运行，不能把它当作生产源对账。

| 现象 | 检查与处理 |
| --- | --- |
| 8090 无法连接 | 检查 Docker、api 容器、端口占用和 DS_BIND_ADDRESS；本项目不监听 8080 看板端口 |
| ready 返回 503 | 查 init/db 状态、配置是否生成、模式是否初始化；不要用删除卷方式修复 |
| worker 无健康心跳 | 查 worker 是否运行、配置/白名单是否合法、是否已有实例持锁、数据库连接是否正常 |
| SOURCE_CONFIG_INVALID / SOURCE_HOST_NOT_ALLOWED | 核对根结构、参数、每表唯一源和白名单，修配置后重建进程 |
| SOURCE_SECRET_MISSING | 检查对应环境变量是否进入容器，不能输出密钥值到共享日志 |
| FIELD_MAPPING_INCOMPLETE | 映射目标集合必须正好覆盖登记的全部字段，即使字段可空 |
| RESPONSE_SCHEMA_CHANGED / INVALID_SOURCE_DATE | 核对已脱敏响应结构、列表路径、页大小和时区转换；不要放宽验证伪造成功 |
| DUPLICATE_RECORD / invalid_record | 核对上游业务键、日期、字段类型、精度、长度；修正源/分片后重试 |
| incomplete_date_range | 缺日尚未成功发布；先补数，或明确接受部分数据后设置 require_complete=false |
| QUERY_TIMEOUT / 413 | 减少查询维度、字段、行数或范围，结合真实数据规划索引和容量；不能无界提高参数 |
| WORKER_INTERRUPTED | 旧发布版本保持，确认进程原因后重新入队；不修改正在发布的事实表 |

本地自动化、独立复核、人工源对账、服务器验收和 CI 是不同证据。尚未执行的环节必须保留“未验证”，不能将本文的可操作步骤写成已通过。

## 1.2.0公式查询升级

仍使用schema3，不新增业务表或迁移记录。先备份数据库与运行配置，再更新api/worker镜像为1.2.0，核对健康检查、原数据行数、发布版本、原接口及新公式预览。已保存公式配置不能由1.1.2解析；回退需将升级前备份恢复到新库并使用旧镜像，不能仅切镜像。升级后新增记录与配置应先单独备份、核对再决定切换，不能覆盖原库或删除数据卷。

## 1.3.0 配置助手升级与回退

本版本镜像1.3.0、数据库schema4。先备份数据库与现有配置、保留1.2.0镜像，确认没有进行中同步/导入；更新api/worker/init为同一版本。初始化只新增`llm_settings`和迁移版本，不改业务表，不提供预设供应商地址/KEY。更新后验证ready、worker、原查询及“大模型配置”空状态。

模型KEY复用现有配置加密密钥，备份数据库时需同时安全保管该环境密钥。首次配置不需要修改Compose或重建容器；模型地址必须容器能访问。测试连接不保存设置；改连接主机、协议、端口需重填或清除KEY。模型被禁用、超时或限流不影响既有手工配置、查询或同步。

1.2.0拒绝直接运行schema4。回滚必须把升级前备份恢复到新的`ds_restore_...`库，用旧镜像核对，再明确切换完整api/worker/init连接。升级后新增的业务记录及模型配置应先备份并核对，不对原库删表/删版本记录，不删数据卷。本地验收只证明独立恢复副本可用，不代表执行过生产切换。
