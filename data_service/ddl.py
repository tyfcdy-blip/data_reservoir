"""受限建表语句元数据解析；仅生成审核草稿，永不执行输入SQL。"""

from dataclasses import dataclass
import re

from pydantic import BaseModel, ConfigDict, ValidationError

from data_service.models import FieldDefinition, identifier

MAX_SQL_BYTES = 65536
MAX_TOKENS = 4096


class DDLParseError(ValueError):
    """可安全展示的解析错误，不携带SQL原文。"""


class DDLPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_name: str
    table_comment: str
    fields: list[FieldDefinition]
    key_fields: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    position: int


def _fail(message: str, position: int) -> DDLParseError:
    return DDLParseError(f"第{position + 1}个字符附近：{message}")


def _tokens(sql: str) -> list[_Token]:
    if not isinstance(sql, str) or not sql.strip():
        raise DDLParseError("请输入一条CREATE TABLE语句")
    try:
        byte_length = len(sql.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DDLParseError("语句包含非法Unicode字符") from exc
    if byte_length > MAX_SQL_BYTES:
        raise DDLParseError("建表语句不得超过64 KiB")
    result: list[_Token] = []
    cursor = 0
    while cursor < len(sql):
        char = sql[cursor]
        if char in " \t\r\n":
            cursor += 1
            continue
        start = cursor
        if char in "'`\"":
            quote = char
            kind = "string" if quote == "'" else "identifier"
            cursor += 1
            value: list[str] = []
            closed = False
            while cursor < len(sql):
                char = sql[cursor]
                cursor += 1
                if char == quote:
                    if cursor < len(sql) and sql[cursor] == quote:
                        value.append(quote)
                        cursor += 1
                        continue
                    closed = True
                    break
                if char == "\\" and kind == "string":
                    if cursor >= len(sql) or sql[cursor] not in "\\'\"":
                        raise _fail("字符串只支持引号和反斜杠转义；请直接输入说明文字", cursor - 1)
                    char = sql[cursor]
                    cursor += 1
                value.append(char)
            if not closed:
                raise _fail("引号未闭合", start)
            result.append(_Token(kind, "".join(value), start))
        elif char.isascii() and (char.isalpha() or char == "_"):
            cursor += 1
            while cursor < len(sql) and sql[cursor].isascii() and (sql[cursor].isalnum() or sql[cursor] == "_"):
                cursor += 1
            result.append(_Token("word", sql[start:cursor], start))
        elif char in "0123456789":
            cursor += 1
            while cursor < len(sql) and sql[cursor] in "0123456789":
                cursor += 1
            if cursor - start > 9:
                raise _fail("数字参数过长", start)
            result.append(_Token("number", sql[start:cursor], start))
        elif char in "(),;=":
            result.append(_Token(char, char, start))
            cursor += 1
        else:
            raise _fail("存在不支持的字符、注释或SQL语法", start)
        if len(result) > MAX_TOKENS:
            raise DDLParseError("语句内容过多，请只保留字段、类型、说明和主键")
    result.append(_Token("end", "", len(sql)))
    return result


class _Parser:
    def __init__(self, sql: str):
        self.tokens = _tokens(sql)
        self.cursor = 0
        self.warnings: list[str] = []
        self.fields: list[FieldDefinition] = []
        self.key_fields: list[str] = []
        self.primary_declared = False
        self.explicit_nullable: set[str] = set()

    @property
    def current(self) -> _Token:
        return self.tokens[self.cursor]

    def accept(self, value: str) -> bool:
        token = self.current
        if token.kind == value or (token.kind == "word" and token.value.upper() == value):
            self.cursor += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise _fail(f"缺少{value}或存在不支持的语法", self.current.position)

    def read_identifier(self) -> str:
        token = self.current
        if token.kind not in {"word", "identifier"}:
            raise _fail("应填写英文字母开头的表名或字段名", token.position)
        try:
            identifier(token.value)
        except ValueError as exc:
            raise _fail("表名和字段名须为字母数字下划线、字母开头且不超过63字符", token.position) from exc
        self.cursor += 1
        return token.value

    def read_comment(self) -> str:
        token = self.current
        if token.kind != "string":
            raise _fail("COMMENT后需要单引号说明文字", token.position)
        self.cursor += 1
        # 与字段模型共用长度和字符规则，表说明同样不可携带控制字符。
        try:
            return FieldDefinition(name="comment", type="string", description=token.value).description
        except ValidationError as exc:
            raise _fail("说明须不超过500字符，且不得包含控制字符", token.position) from exc

    def type_parameters(self, maximum: int = 2) -> list[int]:
        parameters: list[int] = []
        if not self.accept("("):
            return parameters
        while True:
            token = self.current
            if token.kind != "number" or len(parameters) >= maximum:
                raise _fail("类型参数必须是支持范围内的非负整数", token.position)
            parameters.append(int(token.value))
            self.cursor += 1
            if not self.accept(","):
                break
        self.expect(")")
        return parameters

    def field_type(self, name: str) -> tuple[str, int]:
        token = self.current
        if token.kind != "word":
            raise _fail("缺少字段类型", token.position)
        source_type = token.value.upper()
        self.cursor += 1
        if source_type == "DOUBLE":
            self.accept("PRECISION")
        parameters = self.type_parameters()
        if source_type in {"VARCHAR", "CHAR"}:
            if len(parameters) != 1 or parameters[0] < 1:
                raise _fail("VARCHAR和CHAR必须有一个正长度参数", token.position)
            if parameters[0] > 10000:
                self.warnings.append(f"{name}：原字符串长度超过本地上限，草稿设为10000；入库会拒绝超长值，不会截断。")
            return "string", min(parameters[0], 10000)
        if source_type in {"TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT"} and not parameters:
            self.warnings.append(f"{name}：文本草稿最大长度为10000；入库会拒绝超长值，不会截断。")
            return "string", 10000
        if source_type in {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"} and not parameters:
            return "integer", 255
        if source_type in {"FLOAT", "REAL", "DOUBLE"} and not parameters:
            self.warnings.append(f"{name}：浮点类型改为精确decimal(38,6)，需检查源值；超出范围或6位小数会拒绝，不会自动舍入。")
            return "decimal", 255
        if source_type in {"DECIMAL", "NUMERIC"}:
            if parameters and (not 1 <= parameters[0] <= 1000 or
                               len(parameters) == 2 and not 0 <= parameters[1] <= parameters[0]):
                raise _fail("DECIMAL精度或小数位参数无效", token.position)
            self.warnings.append(f"{name}：本地使用decimal(38,6)，最多32位整数和6位小数；超出范围会拒绝，不会自动舍入。")
            return "decimal", 255
        if source_type == "DATE" and not parameters:
            return "date", 255
        if source_type in {"DATETIME", "TIMESTAMP"} and (not parameters or len(parameters) == 1 and parameters[0] <= 6):
            self.warnings.append(f"{name}：日期时间保留为datetime，入库须带明确时区；若作为业务日期，请在审核时选择该字段转为date。")
            return "datetime", 255
        if source_type in {"BOOL", "BOOLEAN"} and not parameters:
            return "boolean", 255
        raise _fail("不支持此字段类型或类型参数，请先简化为支持的类型", token.position)

    def declare_primary(self, names: list[str]) -> None:
        if self.primary_declared:
            raise _fail("只能声明一组PRIMARY KEY", self.current.position)
        if not 1 <= len(names) <= 5 or len(set(names)) != len(names):
            raise _fail("主键须包含1到5个不重复字段", self.current.position)
        self.primary_declared = True
        self.key_fields = names

    def primary_constraint(self) -> None:
        self.expect("KEY")
        self.expect("(")
        names = [self.read_identifier()]
        while self.accept(","):
            if len(names) >= 5:
                raise _fail("主键最多5个字段", self.current.position)
            names.append(self.read_identifier())
        self.expect(")")
        self.declare_primary(names)

    def field(self) -> None:
        if len(self.fields) >= 80:
            raise _fail("最多支持80个字段", self.current.position)
        name = self.read_identifier()
        if name.casefold() in {field.name.casefold() for field in self.fields}:
            raise _fail("字段名称重复（不区分大小写）", self.current.position)
        data_type, max_length = self.field_type(name)
        required = False
        description = ""
        seen: set[str] = set()
        while self.current.kind not in {",", ")", "end"}:
            if self.accept("NOT"):
                option = "nullable"
                self.expect("NULL")
                required = True
            elif self.accept("NULL"):
                option = "nullable"
                self.explicit_nullable.add(name)
            elif self.accept("COMMENT"):
                option = "comment"
                description = self.read_comment()
            elif self.accept("PRIMARY"):
                option = "primary"
                self.expect("KEY")
                self.declare_primary([name])
            else:
                raise _fail("字段仅支持类型、NULL/NOT NULL、COMMENT和PRIMARY KEY", self.current.position)
            if option in seen:
                raise _fail("字段选项重复", self.current.position)
            seen.add(option)
        if not description.strip():
            self.warnings.append(f"{name}：缺少字段说明，请在建表前补充中文说明。")
        self.fields.append(FieldDefinition(name=name, type=data_type, description=description,
                                           required=required, max_length=max_length, dimension=False))

    def parse(self) -> DDLPreview:
        self.expect("CREATE")
        self.expect("TABLE")
        table_name = self.read_identifier()
        self.expect("(")
        if self.current.kind == ")":
            raise _fail("至少需要一个字段", self.current.position)
        while True:
            if self.accept("PRIMARY"):
                self.primary_constraint()
            else:
                self.field()
            if not self.accept(","):
                break
        self.expect(")")
        if not self.fields:
            raise DDLParseError("至少需要一个字段")
        table_comment = ""
        if self.accept("COMMENT"):
            self.accept("=")
            table_comment = self.read_comment()
            if len(table_comment) > 120:
                self.warnings.append("表说明超过120字符，作为本地显示名称时请缩短；字段说明仍完整保留。")
        if self.accept("DISTRIBUTED"):
            self.expect("BY")
            self.expect("RANDOM")
            self.expect("BUCKETS")
            token = self.current
            if token.kind != "number" or not 1 <= int(token.value) <= 1000000:
                raise _fail("BUCKETS须为正整数且不超过1000000", token.position)
            self.cursor += 1
            self.warnings.append("源库的DISTRIBUTED BY RANDOM BUCKETS设置仅被识别，本地不执行分布和分桶配置。")
        self.accept(";")
        self.expect("end")
        fields_by_name = {field.name: field for field in self.fields}
        for key in self.key_fields:
            if key not in fields_by_name:
                raise DDLParseError("PRIMARY KEY引用了不存在的字段")
            if key in self.explicit_nullable:
                raise DDLParseError("PRIMARY KEY字段不可显式声明为NULL")
            fields_by_name[key].required = True
        if not self.key_fields:
            self.warnings.append("语句没有声明PRIMARY KEY，请在审核时明确选择唯一键，不会自动推断。")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", table_name):
            self.warnings.append("源表名不符合本地数据表标识要求，请改为小写字母开头、最长48字符的字母数字下划线。")
        return DDLPreview(table_name=table_name, table_comment=table_comment, fields=self.fields,
                          key_fields=self.key_fields, warnings=self.warnings)


def parse_ddl(sql: str) -> DDLPreview:
    """从一条受限CREATE TABLE语句提取元数据；不推断业务日期、金额口径或主键。"""
    return _Parser(sql).parse()
