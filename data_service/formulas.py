"""受限的聚合后公式：只解析语法树，所有 SQL 标识符与数值均安全构造。"""

import ast
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from psycopg import sql


_DECIMAL_LITERAL = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
_RESULT_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
_OPERATORS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
_MAX_NODES = 80
_MAX_DEPTH = 12


def parse_formula(expression: str) -> ast.Expression:
    """严格白名单；常量从源码取十进制文本，避免经过 Python float 丢精度。"""
    source = expression.strip()
    if not source or len(expression) > 500 or not re.fullmatch(r"[A-Za-z0-9_+*/(). -]+", expression):
        raise ValueError("公式须为500字符以内的单行英文算术表达式")
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise ValueError("公式语法不正确，仅支持汇总别名、十进制数及加减乘除括号") from None
    visited = 0

    def inspect(node: ast.AST, depth: int) -> None:
        nonlocal visited
        visited += 1
        if visited > _MAX_NODES or depth > _MAX_DEPTH:
            raise ValueError("公式过于复杂，请减少运算或括号嵌套")
        if isinstance(node, ast.Expression):
            inspect(node.body, depth + 1)
        elif isinstance(node, ast.Name):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", node.id):
                raise ValueError("公式引用的汇总别名格式不正确")
        elif isinstance(node, ast.Constant):
            literal = ast.get_source_segment(source, node) or ""
            if type(node.value) not in (int, float) or len(literal) > 40 or not _DECIMAL_LITERAL.fullmatch(literal):
                raise ValueError("公式常量仅支持最多40字符的有限十进制数，不支持科学计数法")
        elif isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            inspect(node.left, depth + 1)
            inspect(node.right, depth + 1)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            inspect(node.operand, depth + 1)
        else:
            raise ValueError("公式仅支持汇总别名、十进制数和加减乘除，不支持函数、其他运算或代码")

    inspect(tree, 0)
    return tree


def compile_formula(expression: str, numeric_aliases: set[str], decimal_places: int = 6) -> tuple[sql.Composable, list[Decimal]]:
    """用精确分子/分母表示算式；只在最终输出时按余数四舍五入，避免中间除法丢精度。"""
    if type(decimal_places) is not int or not 0 <= decimal_places <= 12:
        raise ValueError("公式小数位必须为0到12的整数")
    source = expression.strip()
    tree = parse_formula(source)
    one = ("one",)

    def multiply(left: tuple, right: tuple) -> tuple:
        # 仅消去确定非NULL的乘数1，绝不消去0，以保留0*(1/0)的NULL语义。
        if left == one:
            return right
        if right == one:
            return left
        return ("*", left, right)

    def fraction(node: ast.AST) -> tuple[tuple, tuple]:
        if isinstance(node, ast.Name):
            if node.id not in numeric_aliases:
                raise ValueError("公式只能引用本次数字型汇总指标的返回列名，不能引用明细字段、分组或其他公式")
            return ("alias", node.id), one
        if isinstance(node, ast.Constant):
            return ("number", Decimal(ast.get_source_segment(source, node))), one
        if isinstance(node, ast.UnaryOp):
            numerator, denominator = fraction(node.operand)
            return (("negative", numerator) if isinstance(node.op, ast.USub) else numerator), denominator
        left_numerator, left_denominator = fraction(node.left)
        right_numerator, right_denominator = fraction(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            return ((_OPERATORS[type(node.op)], multiply(left_numerator, right_denominator),
                     multiply(right_numerator, left_denominator)), multiply(left_denominator, right_denominator))
        if isinstance(node.op, ast.Mult):
            return multiply(left_numerator, right_numerator), multiply(left_denominator, right_denominator)
        # 每一个原始除法都保留零检查；不能只检查最终分母或代数约掉零因子。
        return multiply(left_numerator, right_denominator), multiply(left_denominator, ("not_zero", right_numerator))

    numerator, denominator = fraction(tree.body)
    parameters: list[Decimal] = []
    expanded_nodes = 0

    def compile_node(node: tuple) -> sql.Composable:
        nonlocal expanded_nodes
        expanded_nodes += 1
        # 分数交叉乘可能放大SQL，先在有界遍历中拒绝，禁止无界展开。
        if expanded_nodes > 2048:
            raise ValueError("公式展开超过2048个计算节点，请减少嵌套除法或拆成更简单的指标")
        kind = node[0]
        if kind == "one":
            return sql.SQL("1::NUMERIC")
        if kind == "alias":
            return sql.SQL("CAST({} AS NUMERIC)").format(sql.Identifier("aggregated", node[1]))
        if kind == "number":
            if len(parameters) >= 512:
                raise ValueError("公式展开超过512个数值参数，请减少重复或嵌套运算")
            parameters.append(node[1])
            return sql.SQL("CAST(%s AS NUMERIC)")
        if kind == "negative":
            return sql.SQL("(-{})").format(compile_node(node[1]))
        if kind == "not_zero":
            return sql.SQL("NULLIF({},0)").format(compile_node(node[1]))
        return sql.SQL("({} {} {})").format(compile_node(node[1]), sql.SQL(kind), compile_node(node[2]))

    factor = ("number", Decimal(10 ** decimal_places))
    quantum = ("number", Decimal((0, (1,), -decimal_places)))
    # DIV与MOD均对NUMERIC做精确整除/余数；一次舍入远离零，最后乘十进制量子。
    # 不先生成有限小数商，因此大数放大、负数中点和双重舍入都不会改变结果。
    parts = [compile_node(node) for node in (
        numerator, denominator,
        numerator, factor, denominator,
        numerator, factor, denominator, denominator,
        quantum,
    )]
    expression_sql = sql.SQL(
        "(SIGN({}) * SIGN({}) * (DIV(ABS({}) * {}, NULLIF(ABS({}),0)) "
        "+ CASE WHEN MOD(ABS({}) * {}, NULLIF(ABS({}),0)) * 2 >= ABS({}) "
        "THEN 1 ELSE 0 END) * {})"
    ).format(*parts)
    if len(expression_sql.as_string().encode("utf-8")) > 65536:
        raise ValueError("公式展开SQL超过64KiB，请减少重复或嵌套运算")
    return expression_sql, parameters


def result_number(value: Any) -> Decimal:
    """结果筛选使用精确数值，不复用源字段NUMERIC(38,6)上限或浮点转换。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("汇总后筛选的数值必须使用整数或十进制字符串，禁止浮点数")
    text = str(value)
    if len(text) > 100 or not _RESULT_NUMBER.fullmatch(text):
        raise ValueError("汇总后筛选必须为100字符以内的有限十进制数，不支持科学计数法")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError("汇总后筛选数值格式不正确") from None
    if not number.is_finite():
        raise ValueError("汇总后筛选必须为有限十进制数")
    return number
