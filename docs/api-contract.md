# 独立数据服务接口与接入说明

版本 1.2.0，2026-10-23。依据本项目 `data_service/api.py`、`models.py`、`datasets.py`、`query.py`、`access.py`、`ingest.py` 和 `source.py` 编写。本项目只提供数据服务；经营看板不属于它的运行依赖。配置和运维见[部署说明](operations-runbook.md)。

## 地址、身份和通用返回

本地默认 `http://127.0.0.1:8090`。`GET /docs` 是不依赖外网的接口目录，`GET /openapi.json` 可导入接口工具；不是图形化数据管理后台。写入和查询请求均使用 `Content-Type: application/json`，JSON 不得含重复键或非有限数值。

| 调用者 | 请求头 | 能力 |
| --- | --- | --- |
| 管理员 | `Authorization: Bearer <登录token>` | 数据集注册、源同步入队、密钥管理、状态检查，也可调试数据接口 |
| 其他系统 | `X-API-Key: <系统密钥>` | 按密钥配置限定接口、数据范围，或使用兼容的整表 `read` / `write` |
| 匿名 | 无 | 首页、健康检查、接口目录和 OpenAPI；不能读业务记录 |

一次数据请求只使用一种凭据，同时提供两种返回 400。`write` 不隐含 `read`，也不能管理源、注册数据集或创建其他密钥。分批导入还校验批次归属，另一个同样有该表写权限的密钥不能操作它。机器密钥应留在调用系统的服务端配置，不嵌入公开浏览器页面。

除健康检查外，成功响应封套为 `{"code":200,"data":...}`；注册和创建批次的 HTTP 状态是 201、同步入队是 202，但封套 `code` 仍为 200。错误使用对应 HTTP 状态：

```json
{"code":409,"error_code":"incomplete_date_range","message":"查询范围存在尚未发布的日期","details":{"missing_dates":["2026-10-20"]}}
```

金额和其他 `decimal` 字段以十进制字符串返回，保留 JSON `null`。日期是 `YYYY-MM-DD`；`datetime` 返回带时区 ISO 时间。整数返回 JSON 整数，可能超出 JavaScript 安全整数范围，调用方应使用支持大整数的解析方式；商品 ID 应定义为字符串，保留前导零。所有示例日期仅作协议示意，实际调用必须改为数据集保留窗口内已发布日期。

## 接口清单

| 方法和路径 | 身份/权限 | 用途 |
| --- | --- | --- |
| `POST /api/v1/auth/login` | 管理员账号密码 | 获取管理会话 |
| `POST /api/v1/auth/logout` | 管理员会话 | 撤销当前会话 |
| `POST /api/v1/admin/datasets` | 管理员 | 注册不可变数据集定义 |
| `POST /api/v1/admin/keys` | 管理员 | 发放接口/范围授权或兼容整表授权的机器密钥 |
| `GET /api/v1/admin/keys` | 管理员 | 最近最多 1000 个密钥的元信息，不返回明文密钥 |
| `DELETE /api/v1/admin/keys/{key_id}` | 管理员 | 撤销密钥，重复撤销同一已有密钥幂等 |
| `POST /api/v1/admin/datasets/{slug}/sync` | 管理员 | 为已配置 HTTP 源按日期范围入队 |
| `GET /api/v1/admin/status` | 管理员 | worker、覆盖范围、最近最多 100 个批次 |
| `GET /api/v1/datasets` | 任一有效身份 | 管理员全部、机器密钥仅有授权的数据集 |
| `GET /api/v1/datasets/{slug}` | 该数据集 `read` | 字段、业务日期、业务主键和保留策略 |
| `POST /api/v1/datasets/{slug}/query` | 该数据集 `read` | 日期范围明细或分组聚合 |
| `POST /api/v1/query/batch` | 全部涉及数据集的 `read` | 同快照批量查询，最多 10 项 |
| `POST /api/v1/datasets/{slug}/batches` | 该数据集 `write` | 创建主动推送批次 |
| `PUT /api/v1/batches/{batch_id}/chunks/{chunk_index}` | 原写入密钥或管理员 | 上传分片 |
| `POST /api/v1/batches/{batch_id}/commit` | 原写入密钥或管理员 | 完整校验后原子发布 |
| `POST /api/v1/batches/{batch_id}/cancel` | 原写入密钥或管理员 | 取消待处理或上传中批次 |
| `GET /api/v1/batches/{batch_id}` | 原写入密钥或管理员 | 检查批次状态 |
| `GET /health/live`、`GET /health/ready` | 匿名 | 进程可达、数据库模式就绪 |

## 管理员与机器密钥

登录请求为 `{"username":"admin","password":"<本地初始化密码>"}`。用户名最长 64 字符，密码最长 1024 字符；初始化密码至少 12 字符。返回 `data.token` 和 `data.expires_in_seconds`，默认会话 8 小时。退出只撤销当前会话。管理员密码重置流程见运维文档。

以下为兼容的完整表授权示例。1.5.0受限接口与行权限使用新增 `policy`，详见[权限契约](key-permissions.md)。

发放机器密钥示例：

```json
{
  "name": "其他系统只读查询",
  "grants": {"product_daily": ["read"], "inventory_daily": ["read"]},
  "expires_in_days": 90,
  "requests_per_minute": 60
}
```

`name` 长度 1～120；`policy`省略或为NULL时，`grants` 必须指定 1～100 个已注册数据集，每表动作取 `read` / `write`，不重复、不为空；`expires_in_days` 为 1～365、默认 90；`requests_per_minute` 为 1～600、默认 60。成功返回 `id,name,token_hint,grants,created_at,expires_at,requests_per_minute,api_key`；**`api_key` 只在创建响应中出现一次**。丢失后重新创建并撤销旧密钥。列表另含 `revoked_at`，不含 `api_key`。限频是每密钥的 60 秒窗口，超限 429；一批最多 10 项的查询作为一次 HTTP 调用计数，仍受结果大小和 SQL 超时约束。

## 注册数据集：表、日期、唯一键与维度

一个数据集对应一张独立的类型化业务表。通过注册 API 传一个下列对象；配置文件 `config/datasets.json` 的根则是这些对象的数组。字段结构、名称及保留策略创建后不自动修改；相同定义重复注册幂等，不同定义返回 409 `dataset_schema_conflict`。需要新结构时注册新 `slug`，再明确迁移和调用方切换。

```json
{
  "slug": "stock_demo",
  "name": "库存日快照模板",
  "date_field": "snapshot_date",
  "key_fields": ["sku", "warehouse"],
  "retention_days": 180,
  "fields": [
    {"name":"snapshot_date","type":"date","required":true,"dimension":true},
    {"name":"sku","type":"string","required":true,"dimension":true,"max_length":120},
    {"name":"warehouse","type":"string","required":true,"dimension":true,"max_length":120},
    {"name":"available_qty","type":"integer"},
    {"name":"unit_cost","type":"decimal"}
  ]
}
```

上面的库存结构只是说明多表能力的假定模板，不代表用户真实库存表。

| 字段 | 限制与含义 |
| --- | --- |
| `slug` | 小写英文字母开头，仅含小写字母、数字、下划线，最多 48 字符；URL 中使用它 |
| `name` | 非空白名称，1～120 字符 |
| `date_field` | 引用已有 `date` 字段，且该字段 `required=true`；一条记录必须与导入批次的业务日期一致 |
| `key_fields` | 1～5 个不重复、存在且必填的字段；唯一性范围为“数据集 + 业务日期 + 这些字段值” |
| `retention_days` | 1～180，默认 180；以北京时间今天为终点，包含今天 |
| `fields` | 1～80 个字段对象，名称不能重复 |
| `fields[].name` | 英文字母开头，仅含字母、数字、下划线，最多 63 字符；不能以下划线开头 |
| `fields[].type` | `string/integer/decimal/date/datetime/boolean` 之一 |
| `fields[].required` | 默认 false；为 true 时不能缺失或为 null |
| `fields[].dimension` | 默认 false；为 true 可用于 `group_by`；`date` 字段无论此值都能分组 |
| `fields[].max_length` | 1～10000，默认 255，仅约束 string 字符数 |

返回对象为 `slug,name,definition,created_at`，不暴露内部物理表名。`definition` 是完整规范化定义，包含省略项的默认值。服务不提供任意 SQL、脚本或跨表 JOIN；支持下述受限四则公式，指标口径由管理员配置。

### 写入类型规则

| 类型 | 接收方式 |
| --- | --- |
| string | 必须为字符串，保留合法首尾空格和前导零；普通字段可为空串，主键不得为空串或全空白；不接受控制字符、非法 Unicode、超长值 |
| integer | JSON 整数或整数字符串，BIGINT 范围；不接受布尔或浮点数 |
| decimal | 推荐十进制字符串，如 `"1234.560000"`；允许整数，禁止 JSON 浮点输入；最多 32 位整数、6 位有效小数，超精度拒绝，不静默舍入 |
| date | 严格 `YYYY-MM-DD`，不自动截取日期时间 |
| datetime | 如 `"2026-10-21T09:00:00+08:00"` 或 Z 结尾，必须含时区；归一为 UTC 后存储；最多微秒精度 |
| boolean | 只接受 JSON `true` / `false`，不将 0、1 或字符串转换为布尔 |

可空字段缺失和显式 null 都保存为 NULL，不补 0；未知字段拒绝。HTTP 拉取适配器可以通过显式字段映射选出子集，以及把指定来源日期时间转为 date，规则见运维文档。

### 商品日示例的口径

[datasets.example.json](../config/datasets.example.json) 第一项选择现有规范商品日表的 **22 个字段子集**，第二项是 8 字段库存模板。商品样例按 `dt + product_id` 唯一；`daily_id` 是源稳定行标识，字符串保留。维度为日期、商品、渠道、店铺、完整类目路径、负责人。店铺跨渠道统计应同时按 `channel,shop_name` 分组，不能仅凭同名店铺合并渠道。

`pay_amount` 是支付金额；`net_sales_amount` 保留源净销售额，服务不再次扣减；`gross_profit` 是毛利润；**维度或周期汇总使用 `profit_amount` 账单利润，单链接单天读取 `profit_pay_est` 支付预估利润**。不把两者混算、不改名为净利润。样例不把源比率相加或直接平均；现在可将已确认口径的分子、分母分别聚合，再在 formulas 中配置二者之比；不会自动猜测或预置利润率、退款率的字段对应关系。

## 查询请求：日期、条件、字段、分组

`POST /api/v1/datasets/{slug}/query` 请求体：

| 参数 | 必填/默认 | 约束 |
| --- | --- | --- |
| `start_date`,`end_date` | 必填 | 严格日期、含两端、正序，最多 180 天，且在该表保留窗内；不能查未来 |
| `fields` | 可省略 | 明细所选字段；省略返回全部业务字段；显式提供须 1～80 项且不重复 |
| `filters` | 默认 `[]` | 最多 20 项，全部 AND；不提供 OR、LIKE 或表达式 |
| `group_by` | 默认 `[]` | 最多 10 项，只能选 dimension 字段或 date 字段 |
| `aggregates` | 默认 `[]` | 最多 20 项；与 group_by 一起组成聚合结果 |
| `formulas` | 默认 `[]` | 最多 10 个聚合后公式，见下文；明细模式不支持 |
| `result_filters` | 默认 `[]` | 最多 20 项数字指标条件，AND，在公式计算后、排序分页前执行 |
| `order_by` | 默认 `[]` | 最多 20 项，`field` 和 `direction:asc/desc`；方向默认 asc |
| `limit` | 默认 100 | 1～500；输出行数，不是上游分页大小 |
| `offset` | 默认 0 | 0～100000 |
| `require_complete` | 默认 true | true 时缺任意发布日期就报 409；false 时返回已发布部分及缺日清单 |

明细模式不能同时提供 `fields` 和非空 `group_by/aggregates`。不传 fields 但提供分组或聚合即进入聚合模式。只分组不聚合返回不同维度组合；只聚合不分组得到全范围一个汇总行。排序在明细模式可用该表任一字段，聚合模式只能用输出分组列、聚合别名或公式别名。排序默认/补充按业务主键和日期、或所有分组字段保证稳定，NULL 始终排最后。

`filters[]` 结构为 `{"field":"channel","op":"eq","value":"天猫"}`。支持 `eq,in,gt,gte,lt,lte,is_null`。`in` 要求 1～200 个非 NULL 值；查询 NULL 使用 `{"field":"profit_amount","op":"is_null","value":true}`，非 NULL 使用 false。字段值按数据集类型校验；字符串精确匹配，不自动 trim。布尔字段只支持 eq/in/is_null。

`aggregates[]` 为 `{"field":"profit_amount","op":"sum","alias":"profit_sum"}`。alias 遵循字段名规则，不得与输出分组列或其他 alias 重名。

| 操作 | 字段要求 | NULL/空集合语义 |
| --- | --- | --- |
| sum / avg | integer 或 decimal | 忽略 NULL，全为 NULL 或没有记录返回 NULL；PostgreSQL 对本服务 BIGINT/NUMERIC 的这些聚合返回 NUMERIC，因此输出十进制字符串 |
| min / max | 除 boolean 外类型 | 忽略 NULL；保留相应类型 |
| count | 可指定字段或省略 field | 指定字段数非 NULL；省略字段相当于 count(*) 数记录；空集合为 0 |
| count_distinct | 必须指定字段 | 统计该字段非 NULL 不同值数量；这不等于多列组合去重，也不等于买家去重 |

### 按周期与店铺汇总

```json
{
  "start_date":"2026-10-01",
  "end_date":"2026-10-20",
  "filters":[{"field":"channel","op":"eq","value":"天猫"}],
  "group_by":["channel","shop_name"],
  "aggregates":[
    {"field":"pay_amount","op":"sum","alias":"pay_sum"},
    {"field":"net_sales_amount","op":"sum","alias":"net_sales_sum"},
    {"field":"profit_amount","op":"sum","alias":"profit_sum"},
    {"op":"count","alias":"record_count"}
  ],
  "order_by":[{"field":"net_sales_sum","direction":"desc"}],
  "limit":5,
  "offset":0,
  "require_complete":true
}
```

维度由调用请求决定：不分组就是选定范围总计；`group_by:["dt"]` 是每天一行；`["channel"]` 是每渠道一行；`["dt","channel","shop_name"]` 是每天每渠道每店一行。不需要为所有起止日期区间分别预存数据。

### 聚合后公式、汇总后筛选与排名（1.2.0）

执行顺序固定为：**日期与原始 filters → 分组聚合 → formulas → result_filters → order_by → limit/offset**。`total` 是汇总后筛选通过的全部组数，公式计算和排序不受分页截断。基础指标和公式指标都在 `columns` / `rows` 返回，响应格式不变。

`formulas[]` 包含 `alias`（唯一返回列名）、`expression`（最多500字符）、`decimal_places`（严格整数0～12，默认6）。公式只可引用当前查询中的**数字型聚合别名**，支持 `+ - * /`、括号、一元正负和有限十进制常量；不能引用原始字段、分组维度或其他公式，不允许函数、SQL、脚本、指数或幂运算。表达式最多80个语法节点、深度12，常量最多40字符；分数运算展开后最多2048个计算节点（包含最终舍入），超限返回422。单个公式展开最多512个参数、64KiB SQL；含前后筛选的整条查询最多16000个参数、1MiB SQL。复杂嵌套除法可触及展开上限，应简化公式。

金额及计算采用精确十进制。公式内部保留精确分子/分母，最终依据商与余数一次舍入，避免先除后乘放大中间误差；聚合输入仍遵循既有SQL聚合语义。每个公式最终按指定小数位四舍五入（中点远离零），随后筛选和排序使用该结果；若需要更细排名，可提高小数位。每处除零返回NULL，缺失值参与运算也返回NULL，不补0；SUM仍忽略部分NULL，全NULL为NULL。比率原值 `0.12` 表示12%，服务不自动乘100；需要12形式时显式写 `refund_sum / paid_sum * 100`。公式结果沿用十进制字符串。

`result_filters` 只支持数字聚合别名和公式别名，结构/运算符同 filters。值使用整数或十进制字符串，不能用JSON小数；`is_null` 使用布尔值，`in` 使用列表；每个数值最多100字符。与原始 filters 分开，可以先筛选渠道再按支付金额门槛筛选汇总组。

以下为**配置示例**：`valid_refund_amount`仅表示此处选择了“有效成功退款”字段，不代表系统替用户决定所有退款率都采用它。创建真实接口前应选定退款口径。

```json
{
  "start_date": "2026-10-01",
  "end_date": "2026-10-20",
  "group_by": ["channel", "shop_name"],
  "aggregates": [
    {"field": "valid_refund_amount", "op": "sum", "alias": "refund_sum"},
    {"field": "paid_amount", "op": "sum", "alias": "paid_sum"}
  ],
  "formulas": [
    {"alias": "refund_rate", "expression": "refund_sum / paid_sum", "decimal_places": 6}
  ],
  "result_filters": [{"field": "paid_sum", "op": "gt", "value": "1000"}],
  "order_by": [{"field": "refund_rate", "direction": "desc"}],
  "limit": 5,
  "offset": 0
}
```

不选分组即返回所选周期总计；按日期分组即每日趋势；多维分组可以做渠道与店铺排名。多项排序按配置顺序，系统补充分组键作为并列排序依据，NULL始终置末。TOP5用`limit=5`；超过一页用`offset`。本版不提供跨表JOIN、同比/环比自动对齐、窗口排名或逐组TOPN，避免将不同语义混入简单公式。

已保存接口的`definition`接受同样的`formulas/result_filters`，预览、直接查询、批量查询和开放服务共用计算器。开放服务调用者只能传日期、白名单原始筛选与分页，不能覆盖公式、后筛和排序。旧请求和旧配置省略新增字段时行为不变。

### 单链接单天明细

```json
{
  "start_date":"2026-10-21",
  "end_date":"2026-10-21",
  "fields":["dt","product_id","product_name","profit_pay_est"],
  "filters":[{"field":"product_id","op":"eq","value":"000123"}],
  "limit":100
}
```

### 查询返回字段

`data` 对象字段如下。下列结构说明不是实际经营数据：

| 返回字段 | 含义 |
| --- | --- |
| `dataset` | 数据集 slug |
| `columns` | 输出列名顺序；rows 中每行是同名字段对象 |
| `rows` | 明细行或分组聚合行数组，最多 limit 行；金额字符串、NULL 原样 |
| `total` | 过滤后全部明细行数或聚合组数，不只是本页数量 |
| `limit`,`offset` | 本次分页参数 |
| `coverage.complete` | 查询范围内所有日期均有发布记录才为 true，不受 filters 是否匹配到记录影响 |
| `coverage.missing_dates` | 没有成功发布的日期列表 |
| `published_versions[]` | 每个已发布日的 `{date,batch_id,row_count}`；row_count 是该日全部记录数，不是过滤后的数量 |

完整发布的零行日期不算缺日。即使筛选条件没有记录，全范围无分组聚合仍可返回一行 SUM 为 NULL、COUNT 为 0 的结果，`total` 为 1；有分组且无记录则没有结果组。

单结果封套及 UTF-8 JSON 内容最多 **8 MiB**，批量响应最多 **16 MiB**；超限返回 413，不返回截断成功结果。遇到上限应减少字段/limit 或拆分请求。跨多次分页请求期间可能发生新发布，应比较 `published_versions`；本版没有固定版本导出游标，不能承诺多次 HTTP 请求天然同版。

## 跨表批量查询

`POST /api/v1/query/batch` 的 `queries` 为 1～10 项：

```json
{
  "queries":[
    {"dataset":"product_daily","query":{
      "start_date":"2026-10-21","end_date":"2026-10-21",
      "aggregates":[{"field":"profit_amount","op":"sum","alias":"profit_sum"}]
    }},
    {"dataset":"inventory_daily","query":{
      "start_date":"2026-10-21","end_date":"2026-10-21",
      "group_by":["warehouse"],
      "aggregates":[{"field":"available_qty","op":"sum","alias":"available_sum"}]
    }}
  ]
}
```

`data` 是与请求顺序一致的查询结果数组，每项使用上一节返回结构。同一个数据库 REPEATABLE READ 只读快照中执行；任一数据集无权限、缺日、参数错误、超时或超大小，整个请求失败，不返回部分成功。**它是多个独立查询的组合，不做跨表关联、拼接或 JOIN。** 库存是时点量，样例只查询单天，不把多日库存求和当作当前库存。

## 主动推送：按日完整替换

推送不是逐行 upsert。调用方要先获得某日完整快照，声明总行数，再分批上传；全部通过后才发布。同日重导完成时替换这一天的所有旧记录，包括移除上游已删除的记录。上传途中和失败后查询仍读旧成功版本。

1. `POST /api/v1/datasets/inventory_daily/batches`：

```json
{"business_date":"2026-10-21","expected_rows":2,"idempotency_key":"inventory_20261021_export_v1"}
```

`expected_rows` 必填，整数 0～1000000。`idempotency_key` 必填，1～120 字符，仅英文字母数字及 `_ . : -`；唯一范围是数据集。相同 key、日期、行数、身份重复创建返回原批次；不同参数返回 409。同表同日仅允许一个 pending/running/uploading 批次。将返回的 `data.id` 用于后续路径。

2. `PUT /api/v1/batches/{batch_id}/chunks/0`：

```json
{
  "rows":[
    {"snapshot_date":"2026-10-21","sku":"000123","warehouse":"示例仓A","available_qty":8,"reserved_qty":2,"unit_cost":"12.340000","observed_at":"2026-10-21T23:00:00+08:00","active":true},
    {"snapshot_date":"2026-10-21","sku":"000124","warehouse":"示例仓A","available_qty":0,"reserved_qty":0,"unit_cost":null,"observed_at":null,"active":false}
  ]
}
```

每片 1～1000 行，序号 0～999999；还受默认 2 MiB 请求体限制。最终序号须从 0 连续，可分多片上传；相同序号重发规范化后同内容不重复入库，不同内容 409。分片内或跨分片重复业务键返回 409，当前分片整体回滚，既有分片保持；修正后可以重试。单行日期、类型或字段错误同样拒绝当前分片，不会跳过坏行。

3. `POST /api/v1/batches/{batch_id}/commit`，不带请求体。服务校验总行数与分片连续性后原子发布；成功状态 `succeeded`。重复 commit 成功批次幂等；行数不足或缺片返回 409，补齐后可重试。明确零行日用 `expected_rows:0` 创建后直接 commit，不上传空分片。

4. 可用 `GET /api/v1/batches/{batch_id}` 复核状态。取消用 `POST .../cancel`，允许 pending/uploading，删除本批未发布暂存；不能取消成功或运行中的拉取批次。成功发布后不提供“回到历史批次”API，因为旧事实已清理；需重新完整导入或走数据库备份恢复。

批次返回字段：`id,dataset_slug,business_date,mode,status,owner_key_id,expected_rows,row_count,page_count,idempotency_key,created_at,updated_at,started_at,finished_at,error_code,error_message`。`mode` 为 push/pull；push 初始 uploading，pull 初始 pending；终态 succeeded/failed/cancelled。`page_count` 是接收分片数；拉取内部还记录结束空页，不能把它当作业务行数。没有发生的时间/错误是 null。上传超过 24 小时没有活动会被清理标记失败。

## HTTP 拉取入队和运行状态

HTTP 来源由管理员页面或运维配置文件登记，普通调用系统不能提交任意源 URL。管理员在源已配置、字段定义已注册后调用：

```json
{"start_date":"2026-10-01","end_date":"2026-10-20"}
```

路径为 `POST /api/v1/admin/datasets/product_daily/sync`。范围含两端，最多 180 天、在该数据集保留窗内、只接受昨天及以前。返回 HTTP 202 和批次数组只代表排队；通过状态接口检查每个日期是否 `succeeded`。入队事务全成或全败；同日存在活动批次时 409，不悄悄只排其中部分。手工重复请求不带幂等键：已有任务完成后可重新入队，形成同日新版本。

`GET /api/v1/admin/status` 返回：

| 字段 | 内容 |
| --- | --- |
| `workers` | `worker_key,status,heartbeat_at,healthy`；健康由数据库时间检查最近 90 秒心跳 |
| `recent_batches` | 最新最多 100 条批次元信息，结构同上 |
| `coverage` | 每数据集 `dataset_slug,first_date,last_date,published_days,row_count`；没有已发布日的数据集不出现 |
| `configured_sources` | 已配置源对应的数据集 slug 列表，不返回 URL 或密钥 |
| `schedule_enabled` | 是否启用每日调度 |

first/last_date 不能证明中间无缺日，准确完整性以查询返回 coverage 为准。HTTP GET/POST、字段映射、参数名、分页、签名和时区配置参见[配置与部署](operations-runbook.md)。

## 错误处理约定

| HTTP 状态 | 常见原因/处理 |
| --- | --- |
| 400 | `AMBIGUOUS_CREDENTIALS`：不要同时传管理员与机器凭据 |
| 401 | 会话/机器密钥缺失、过期、已撤销或无效 |
| 403 | `DATASET_FORBIDDEN` / `BATCH_FORBIDDEN`：逐表动作或批次归属不允许 |
| 404 | 数据集、批次或密钥不存在 |
| 409 | 模式不兼容、缺日期、活动批次冲突、重复主键、片冲突、行数/片不完整或状态不可操作 |
| 413 | 请求体、单查询 8 MiB 或批量 16 MiB 大小超限 |
| 422 | 未知字段、类型、格式、范围、精度或 JSON 不合法；检查请求定义后重试 |
| 429 | 管理员登录或机器密钥频率限制，延迟重试，不能无界循环 |
| 503 | 数据库不可用、未初始化、源配置无效等依赖故障 |
| 504 | 单条 SQL 超时；缩小范围/维度后重试，默认 15 秒不是整个批量请求总时限 |

调用方以 HTTP 状态和稳定 `error_code` 判断，不解析中文 message。参数和数据库错误不回显密钥或原始业务值；响应的 `X-Request-ID` 可辅助定位，但不能宣称已有跨进程链路追踪系统。

## 管理员页面与命名查询服务（1.1.0新增）

页面 `/admin/`，完整新增接口模型已纳入 `/openapi.json`。管理来源：GET `/api/v1/admin/sources`；GET/PUT `/api/v1/admin/sources/{dataset_slug}`；POST `/api/v1/admin/sources/{dataset_slug}/test`。管理加工：GET `/api/v1/admin/query-services`；GET/PUT `/api/v1/admin/query-services/{slug}`；POST `/api/v1/admin/query-services/preview`。外部目录：GET `/api/v1/services`；固定查询：POST `/api/v1/services/{slug}/query`。请求字段、修订与返回结构见[共享契约](admin-console-plan.md)，调用样例与权限边界见[管理员使用说明](admin-guide.md)。原逐表明细/聚合/批量及推送接口保持兼容。


## 1.1.2 来源多时间与写入方式

管理员 `PUT /api/v1/admin/sources/{dataset_slug}` 在原有请求字段外支持：

| 字段 | 约束与兼容规则 |
| --- | --- |
| schedule_times | 1～24项字符串数组，严格HH:mm北京时间，不重复，自动升序，例如["03:00","12:30","20:00"]；省略兼容旧schedule_hour；两者均未提供时保留已保存日程；显式null拒绝 |
| schedule_hour | 旧字段0～23，仍接受；响应返回首个时间的小时，分钟应读取schedule_times |
| write_mode | upsert或replace_day；省略时保留已有方式，新建服务端默认replace_day；页面新建默认选择upsert；显式null拒绝 |

返回包含规范化后的schedule_times、write_mode和既有revision。请求仍须使用当前revision，不能省略已有鉴权规则。修改时 secrets={} 可保留原凭据。

定时与手动拉取任务在入队时固定write_mode。batches响应增加write_mode与published_row_count；row_count仍为本次接收数，published_row_count仅成功后为最终发布总数，之前为null。旧成功批次在升级时按原row_count回填。push创建接口未改变，默认仍整日替换。

upsert只在同一数据表、同一业务日期内按登记业务键匹配。相同键用本次规范化完整行覆盖（含NULL），本次未返回的键保留。全部分页完成并通过校验后统一切换发布版本；新增、保留、切换与旧版本删除在同一事务内。空响应可保留旧记录，但覆盖信息表示本服务完成本次拉取，不保证上游自身提供一致性快照。

## 1.3.0 管理员模型连接与自然语言草稿

以下路由只接受管理员会话，不接受机器调用密钥。既有开放服务查询协议不变。供应商协议基于[Chat Completions官方说明](https://developers.openai.com/api/reference/resources/chat)，实现仅支持非流式、单条message.content文本中的严格JSON，不保证兼容所有标称OpenAI兼容的供应商。

| 路由 | 用途 |
| --- | --- |
| GET /api/v1/admin/llm | 读取安全配置，只返回has_api_key，不返回明文或密文 |
| PUT /api/v1/admin/llm | 保存模型配置，revision乐观锁 |
| POST /api/v1/admin/llm/test | 按当前输入测试连接，不保存；调用模型要求返回受控JSON |
| POST /api/v1/admin/query-services/draft | 仅生成当前表的聚合查询草稿，不写规则，不查业务记录 |

保存和连接测试字段：`base_url`（最多2048字符）、`model`（最多200字符）、可选`api_key`、`clear_api_key=false`、`enabled=false`、`timeout_seconds=45`（5～90）、`token_parameter=max_tokens`（或max_completion_tokens）、`revision`（首次0，后续使用读取值）。省略api_key保留旧值；提供新值替换；clear_api_key=true清除，不能与新KEY同时使用。更换地址origin后不得沿用旧KEY。GET新库返回空地址/模型、has_api_key=false、enabled=false、revision=0。

草稿请求示例：

```json
{"dataset_slug":"product_day","prompt":"按shop_name、category、channel、owner分组，支付金额paid_amount求和、有效退款valid_refund_amount求和，退款率为两个汇总值相除，按退款率降序。调用方传日期范围。"}
```

prompt为1～4000字符。响应在通用`data`内包含：`status`、`name`、`slug`、`definition`、`allowed_filter_fields`、`max_limit`、`questions`、`notes`。status为ready时definition是已有QueryTemplate，必须有聚合指标且没有待确认问题；needs_clarification时definition=null且questions非空，不允许应用到表单。前后端保留原定义校验，不允许额外SQL、执行指令、跨表或启用状态字段。公式仅引用数值汇总别名，不可引用另一公式。

模型未配置/未启用409；旧revision409；已有模型请求429；供应商限流/额度503；连接/响应/草稿无效502；超时504。错误只提供脱敏说明，不返回上游原文及KEY。任何模型调用不自动重试。每个服务数据库同一时刻最多一个模型请求；锁在失败时释放。


## 1.4.0 DDL预览与显式文本转换

管理员POST `/api/v1/admin/datasets/ddl-preview`，请求`{"sql":"CREATE TABLE ..."}`。成功data包含`table_name`、`table_comment`、`fields`（现有FieldDefinition数组）、`key_fields`、`warnings`。64 KiB/80列限制，非法或不支持语法422，无管理员会话401；不接受机器密钥，不写数据库。创建仍调用原`POST /api/v1/admin/datasets`；原始DDL永不执行。

SourceConfig新增可省略的`text_whitespace_fields: string[]`，默认`[]`，最多80个不重复目标字段，只能选择数据表string字段。映射完成后将所选字段的CRLF、CR、LF、TAB替换为空格，NULL保留、其他内容不变。字段缺失或返回非文本报错，整日不发布；管理入口和worker均核验目标类型。具体表名/字段与中文说明以示例定义为准。新参数向旧1.3.0回退需恢复版本匹配的配置库，参见运行说明。


## 1.5.0 密钥接口范围与行权限

新增可选 `policy`；完整结构、只读限制、组合逻辑、旧调用兼容和迁移恢复边界见[权限配置说明](key-permissions.md)。直接查询、批量和固定接口复用同一个权限SQL编译器。受限查询的 `total` 与 `published_versions.row_count` 不返回未授权店铺行数；无权访问接口/表或失效权限返回403。
