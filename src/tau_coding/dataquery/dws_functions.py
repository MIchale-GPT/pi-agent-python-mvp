"""Documented DWS 9.1.0 read-only function additions and parser spellings.

Names describe application policy admission, not server version/type checking.
Sources are retained alongside the catalog for review and the static admin page.
"""

from __future__ import annotations

DWS_DOCUMENTATION = "https://support.huaweicloud.com/intl/en-us/sqlreference-dws/"

DWS_FUNCTION_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "正则匹配与清洗",
        "dws_06_0033.html",
        (
            "REGEXP_LIKE",
            "REGEXP_REPLACE",
            "REGEXP_MATCHES",
            "REGEXP_SPLIT_TO_ARRAY",
            "REGEXP_SPLIT_TO_TABLE",
            "REGEXP_SUBSTR",
        ),
    ),
    (
        "金额与类型转换",
        "dws_06_0036.html",
        (
            "TO_NUMBER",
            "TRY_CAST",
            "HEXTORAW",
            "RAWTOHEX",
            "NUMTODAY",
            "TO_CLOB",
        ),
    ),
    (
        "日期计算",
        "dws_06_0309.html",
        (
            "ADDDATE",
            "ADDTIME",
            "ADD_MONTHS",
            "LAST_DAY",
            "NEXT_DAY",
            "FROM_DAYS",
            "TO_DAYS",
            "CLOCK_TIMESTAMP",
            "STATEMENT_TIMESTAMP",
            "TRANSACTION_TIMESTAMP",
            "LOCALTIME",
            "LOCALTIMESTAMP",
            "PG_SYSTIMESTAMP",
        ),
    ),
    (
        "数值计算",
        "dws_06_0307.html",
        (
            "ACOS",
            "ASIN",
            "ATAN",
            "ATAN2",
            "BITAND",
            "CBRT",
            "COS",
            "SIN",
            "TAN",
            "COT",
            "DEGREES",
            "RADIANS",
            "DIV",
            "PI",
            "RANDOM",
            "RAND",
            "WIDTH_BUCKET",
        ),
    ),
    (
        "聚合分析",
        "dws_06_0046.html",
        (
            "MEDIAN",
            "PERCENTILE_CONT",
            "PERCENTILE_DISC",
            "LISTAGG",
            "BIT_AND",
            "BIT_OR",
            "REGR_AVGX",
            "REGR_AVGY",
            "REGR_COUNT",
            "REGR_INTERCEPT",
            "REGR_R2",
            "REGR_SLOPE",
            "REGR_SXX",
            "REGR_SXY",
            "REGR_SYY",
        ),
    ),
)

DWS_FUNCTIONS: frozenset[str] = frozenset(
    name for _, _, names in DWS_FUNCTION_GROUPS for name in names
)

# sqlglot's AST names are not necessarily source/database function names.
# Only map known nodes; anonymous/user-defined function names are never rewritten.
DWS_AST_FUNCTION_NAMES: dict[str, str] = {
    "REGEXP_I_LIKE": "REGEXP_LIKE",
    "STR_TO_DATE": "TO_DATE",
    "STR_TO_TIME": "TO_TIMESTAMP",
    "UNIX_TO_TIME": "TO_TIMESTAMP",
    "TIME_TO_STR": "TO_CHAR",
    "GROUP_CONCAT": "STRING_AGG",
    "TRUNCATE": "TRUNC",
    "POW": "POWER",
    "BITWISE_AND_AGG": "BIT_AND",
    "BITWISE_OR_AGG": "BIT_OR",
    "LOGICAL_AND": "BOOL_AND",
    "LOGICAL_OR": "BOOL_OR",
    "RAND": "RANDOM",
    "EXPLODING_GENERATE_SERIES": "GENERATE_SERIES",
    "J_S_O_N_ARRAY_AGG": "JSON_AGG",
    "STR_POSITION": "POSITION",
    "VARIANCE_POP": "VAR_POP",
    "CAST_TO_STR_TYPE": "CAST",
}
