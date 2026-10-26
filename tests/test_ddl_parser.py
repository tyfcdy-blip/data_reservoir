"""DDL01/DDL02：受限SQL词法、类型风险、说明保真、有界输入与恶意语法拒绝。"""

import pytest

from data_service.ddl import DDLParseError, MAX_SQL_BYTES, parse_ddl
from data_service.models import DatasetDefinition

DASHBOARD_DDL = """CREATE TABLE `ads_product_dashboard_daily` (
  `daily_id` VARCHAR(1048576) COMMENT '稳定商品日行ID；LOWER(MD5(daily_id_v1规范化数据自然日+商品ID))',
  `dt` DATETIME COMMENT '数据日期；daily_id按其自然日部分计算',
  `product_id` VARCHAR(1048576) COMMENT '商品ID；与数据自然日共同构成已确认业务唯一键',
  `product_name` VARCHAR(1048576) COMMENT '商品名称',
  `product_image` VARCHAR(1048576) COMMENT '商品图片',
  `product_tag` VARCHAR(1048576) COMMENT '商品标签',
  `channel` VARCHAR(1048576) COMMENT '渠道',
  `shop_name` VARCHAR(1048576) COMMENT '店铺名称',
  `category` VARCHAR(1048576) COMMENT '类目',
  `owner_person` VARCHAR(1048576) COMMENT '负责人',
  `pay_amount` DOUBLE COMMENT '支付金额',
  `net_sales_amount` VARCHAR(1048576) COMMENT '净销售额',
  `sales_qty` DOUBLE COMMENT '销售件数',
  `aov` DOUBLE COMMENT '客单价',
  `real_aov` DOUBLE COMMENT '真实客单价',
  `refund_amount` DOUBLE COMMENT '退款金额',
  `refund_qty` DOUBLE COMMENT '退款件数',
  `refund_rate` DOUBLE COMMENT '退款率',
  `shipped_refund_rate` DOUBLE COMMENT '已发货退款率',
  `shipped_refund_amount_ratio` DOUBLE COMMENT '已发货退款金额占比',
  `return_rate` DOUBLE COMMENT '退货率',
  `goods_cost` DOUBLE COMMENT '货品成本',
  `net_goods_cost` DOUBLE COMMENT '净货品成本',
  `goods_cost_ratio` DOUBLE COMMENT '货品成本占比',
  `net_goods_cost_ratio` DOUBLE COMMENT '净货品成本占比',
  `promotion_cost` DOUBLE COMMENT '推广花费',
  `promotion_cost_ratio` DOUBLE COMMENT '推广花费占比',
  `promotion_cost_net_sales_ratio` DOUBLE COMMENT '推广花费占净销售额比例',
  `promotion_cost_net_ship_amount_ratio` DOUBLE COMMENT '推广花费占净发货金额比例',
  `platform_cost` DOUBLE COMMENT '平台花费',
  `platform_cost_ratio` DOUBLE COMMENT '平台花费占比',
  `ship_fee` DOUBLE COMMENT '发货费用',
  `sales_fee` DOUBLE COMMENT '销售费用',
  `custom_fee` DOUBLE COMMENT '自定义费用',
  `custom_fee_ratio` DOUBLE COMMENT '自定义费用占比',
  `absolute_shared_ratio` DOUBLE COMMENT '绝对公摊占比',
  `gross_profit` DOUBLE COMMENT '毛利额',
  `gross_profit_rate` DOUBLE COMMENT '毛利率',
  `profit_amount` DOUBLE COMMENT '利润额',
  `profit_rate` DOUBLE COMMENT '利润率',
  `tao_product_exp_score` DOUBLE COMMENT '淘系商品体验分',
  `product_exp_score` VARCHAR(1048576) COMMENT '商品体验分',
  `shop_overall_exp_score` VARCHAR(1048576) COMMENT '店铺综合体验分',
  `trade_in_self_subsidy_bridge` DOUBLE COMMENT '以旧换新自补平台过桥',
  `merchant_subsidy` DOUBLE COMMENT '商家出资补贴',
  `profit_est` DOUBLE COMMENT '利润（预估）',
  `profit_pay_est` DOUBLE COMMENT '利润(支付预估)',
  `profit_rate_est_new` DOUBLE COMMENT '利润率（预估）（新）',
  `profit_rate_pay_est_new` DOUBLE COMMENT '利润率（支付预估）（新）',
  `listing_time` DATETIME COMMENT '上架时间'
)
COMMENT 'ADS层商品驾驶舱规范日表；移除净利润字段并增加稳定行ID'
DISTRIBUTED BY RANDOM BUCKETS 4"""


def test_complete_user_table_preserves_all_fields_and_business_ambiguities():
    preview = parse_ddl(DASHBOARD_DDL)
    assert preview.table_name == "ads_product_dashboard_daily"
    assert len(preview.fields) == 50
    assert len({field.name for field in preview.fields}) == 50
    assert all(field.description for field in preview.fields)
    fields = {field.name: field for field in preview.fields}
    assert fields["daily_id"].description == "稳定商品日行ID；LOWER(MD5(daily_id_v1规范化数据自然日+商品ID))"
    assert fields["profit_rate_pay_est_new"].description == "利润率（支付预估）（新）"
    assert fields["dt"].type == fields["listing_time"].type == "datetime"
    assert fields["net_sales_amount"].type == "string"
    assert fields["product_id"].max_length == 10000
    assert fields["sales_qty"].type == fields["refund_qty"].type == "decimal"
    assert not any(field.required or field.dimension for field in preview.fields)
    assert preview.key_fields == []
    assert any("PRIMARY KEY" in warning for warning in preview.warnings)
    assert any("不会自动舍入" in warning for warning in preview.warnings)
    assert any("不会截断" in warning for warning in preview.warnings)
    assert any("分桶" in warning for warning in preview.warnings)


def test_preview_can_be_reviewed_into_existing_dataset_model():
    preview = parse_ddl(DASHBOARD_DDL)
    fields = [field.model_dump() for field in preview.fields]
    for field in fields:
        if field["name"] == "dt":
            field.update(type="date", required=True, dimension=True)
        if field["name"] == "daily_id":
            field["required"] = True
        if field["name"] == "net_sales_amount":
            field["type"] = "decimal"
    definition = DatasetDefinition(slug=preview.table_name, name="商品驾驶舱日表", date_field="dt",
                                   key_fields=["daily_id"], fields=fields)
    assert len(definition.fields) == 50
    assert definition.key_fields == ["daily_id"]


@pytest.mark.parametrize("sql_type,expected,length", [
    ("VARCHAR(14)", "string", 14), ("CHAR(3)", "string", 3), ("TEXT", "string", 10000),
    ("TINYTEXT", "string", 10000), ("MEDIUMTEXT", "string", 10000), ("LONGTEXT", "string", 10000),
    ("INT", "integer", 255), ("INTEGER", "integer", 255), ("BIGINT", "integer", 255),
    ("SMALLINT", "integer", 255), ("TINYINT", "integer", 255),
    ("DOUBLE", "decimal", 255), ("DOUBLE PRECISION", "decimal", 255), ("FLOAT", "decimal", 255),
    ("REAL", "decimal", 255), ("DECIMAL", "decimal", 255), ("NUMERIC(18,2)", "decimal", 255),
    ("DECIMAL(20)", "decimal", 255), ("DECIMAL(60,30)", "decimal", 255),
    ("DATE", "date", 255), ("DATETIME", "datetime", 255), ("TIMESTAMP(6)", "datetime", 255),
    ("DATETIME(0)", "datetime", 255), ("BOOL", "boolean", 255), ("BOOLEAN", "boolean", 255),
])
def test_supported_types_map_to_fixed_local_types(sql_type, expected, length):
    field = parse_ddl(f"CREATE TABLE t (a {sql_type} COMMENT '说明')").fields[0]
    assert field.type == expected
    assert field.max_length == length


def test_quoted_comment_delimiters_and_statement_text_are_only_data():
    sql = """cReAtE TaBlE `sample` (
    `key` VARCHAR(30) NOT NULL COMMENT '英文''引号, 反引号`，圆括号()；分号; DROP TABLE x;',
    "day" DATE COMMENT '日期',
    PRIMARY KEY (`key`)
    ) COMMENT='表,说明;（保留）';"""
    preview = parse_ddl(sql)
    assert preview.fields[0].description == "英文'引号, 反引号`，圆括号()；分号; DROP TABLE x;"
    assert preview.table_comment == "表,说明;（保留）"
    assert preview.key_fields == ["key"]
    assert preview.fields[0].required


def test_escaped_quote_and_backslash_in_comment():
    preview = parse_ddl(r"CREATE TABLE t (a TEXT COMMENT '引号\'和路径C:\\root')")
    assert preview.fields[0].description == "引号'和路径C:\\root"


def test_table_and_column_primary_keys_become_required_only_when_declared():
    preview = parse_ddl("CREATE TABLE t (a INT COMMENT '主键', b DATE COMMENT '日期', PRIMARY KEY (a,b))")
    assert preview.key_fields == ["a", "b"]
    assert all(field.required for field in preview.fields)
    assert parse_ddl("CREATE TABLE t (a INT PRIMARY KEY COMMENT '主键')").key_fields == ["a"]
    explicit_nullable = parse_ddl("CREATE TABLE t (a INT NULL COMMENT '可空')").fields[0]
    assert not explicit_nullable.required


def test_missing_comment_is_a_review_warning_and_long_table_name_is_not_silently_changed():
    preview = parse_ddl("CREATE TABLE MixedCaseTable (a INT)")
    assert preview.table_name == "MixedCaseTable"
    assert preview.fields[0].description == ""
    assert any("中文说明" in warning for warning in preview.warnings)
    assert any("最长48" in warning for warning in preview.warnings)


def test_exact_limits_accept_80_fields_500_character_comment_and_64k_input():
    body = ",".join(f"f{number} INT COMMENT '字段'" for number in range(80))
    assert len(parse_ddl(f"CREATE TABLE t ({body})").fields) == 80
    description = "中" * 500
    assert parse_ddl(f"CREATE TABLE t (a TEXT COMMENT '{description}')").fields[0].description == description
    basic = "CREATE TABLE t (a INT)"
    assert parse_ddl(basic + " " * (MAX_SQL_BYTES - len(basic))).table_name == "t"
    with pytest.raises(DDLParseError, match="64 KiB"):
        parse_ddl(basic + " " * (MAX_SQL_BYTES - len(basic) + 1))
    with pytest.raises(DDLParseError, match="64 KiB"):
        parse_ddl(basic + "中" * 22000)


@pytest.mark.parametrize("statement", [
    "", "   ", "SELECT 1", "CREATE TABLE t AS SELECT 1", "CREATE TEMP TABLE t (a INT)",
    "CREATE TABLE IF NOT EXISTS t (a INT)", "CREATE TABLE db.t (a INT)",
    "CREATE TABLE t ()", "CREATE TABLE t (a INT,)", "CREATE TABLE t (a INT, A INT)",
    "CREATE TABLE t (a INT); DROP TABLE datasets;", "CREATE TABLE t (a INT); CREATE TABLE x (b INT)",
    "CREATE TABLE t (a INT);;", "CREATE TABLE t (a INT) ENGINE=InnoDB", "CREATE TABLE t (a JSON)",
    "CREATE TABLE t (a INT DEFAULT 3)", "CREATE TABLE t (a INT CHECK (a>0))", "CREATE TABLE t (a INT UNSIGNED)",
    "CREATE TABLE t (a INT GENERATED ALWAYS AS (1))", "CREATE TABLE t (a INT, FOREIGN KEY (a) REFERENCES x(a))",
    "CREATE TABLE t (a INT, UNIQUE(a))", "CREATE TABLE t (a INT) AS SELECT 1", "CREATE TABLE t (a INT) INTO OUTFILE 'x'",
    "CREATE TABLE t (a VARCHAR)", "CREATE TABLE t (a VARCHAR(0))", "CREATE TABLE t (a VARCHAR(-1))",
    "CREATE TABLE t (a VARCHAR(2,1))", "CREATE TABLE t (a VARCHAR(1e6))", "CREATE TABLE t (a INT(11))",
    "CREATE TABLE t (a DECIMAL(0,0))", "CREATE TABLE t (a DECIMAL(3,4))", "CREATE TABLE t (a DECIMAL(1001,0))",
    "CREATE TABLE t (a DECIMAL(3,2,1))", "CREATE TABLE t (a DATETIME(7))", "CREATE TABLE t (a TIMESTAMP WITH TIME ZONE)",
    "CREATE TABLE t (a INT NOT NULL NULL)", "CREATE TABLE t (a INT COMMENT 'x' COMMENT 'y')",
    "CREATE TABLE t (a INT PRIMARY KEY, b INT PRIMARY KEY)", "CREATE TABLE t (a INT NULL PRIMARY KEY)",
    "CREATE TABLE t (a INT PRIMARY KEY, PRIMARY KEY(a))", "CREATE TABLE t (a INT, PRIMARY KEY(z))",
    "CREATE TABLE t (a INT, PRIMARY KEY(a,a))", "CREATE TABLE t (a INT, PRIMARY KEY())",
    "CREATE TABLE t (a INT COMMENT 'unfinished)", "CREATE TABLE t (a INT COMMENT \"wrong quotes\")",
    "CREATE TABLE t (a INT COMMENT 'bad\\q')", "CREATE TABLE t (a INT COMMENT 'bad\ncomment')",
    "CREATE TABLE t (a INT COMMENT 'bad\x00comment')", "CREATE TABLE t (a INT COMMENT '\ud800')",
    "CREATE TABLE t (`bad``key` INT)", "CREATE TABLE t (`a-b` INT)", "CREATE TABLE t (_reserved INT)",
    "CREATE TABLE t (a INT) -- trailing", "/*comment*/ CREATE TABLE t (a INT)", "CREATE TABLE t (a INT # comment\n)",
    "CREATE TABLE t (a INT) DISTRIBUTED BY HASH(a) BUCKETS 4", "CREATE TABLE t (a INT) DISTRIBUTED BY RANDOM BUCKETS 0",
    "CREATE TABLE t (a INT) DISTRIBUTED BY RANDOM BUCKETS 1000001", "CREATE TABLE t (a INT) DISTRIBUTED BY RANDOM BUCKETS 4 EXTRA",
    "CREATE TABLE t (a INT) COMMENT 'x' COMMENT 'y'", "CREATE TABLE t (a INT) COMMENT 'x\tbad'",
    "CREATE TABLE t (a VARCHAR(9999999999))", "CREATE TABLE t (a TEXT COMMENT '" + "中" * 501 + "')",
    "CREATE TABLE " + "a" * 64 + " (b INT)", "CREATE TABLE t (" + "a" * 64 + " INT)",
    "CREATE TABLE t (" + ",".join(f"a{number} INT" for number in range(81)) + ")",
    "CREATE TABLE t (" + ",".join(f"a{number} INT" for number in range(6)) + ",PRIMARY KEY(a0,a1,a2,a3,a4,a5))",
])
def test_unsupported_malformed_or_executable_syntax_is_rejected(statement):
    with pytest.raises(DDLParseError):
        parse_ddl(statement)


def test_excessive_tokens_fail_before_semantic_work():
    with pytest.raises(DDLParseError, match="内容过多"):
        parse_ddl("CREATE TABLE t (" + "a " * 4097 + ")")


def test_comment_can_contain_html_without_execution_or_metadata_loss():
    description = "<img src=x onerror=alert(1)>支付金额"
    preview = parse_ddl(f"CREATE TABLE t (a DOUBLE COMMENT '{description}')")
    assert preview.fields[0].description == description
