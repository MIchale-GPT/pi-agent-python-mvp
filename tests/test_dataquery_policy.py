"""Table-driven SQL policy tests (PRD testing decision 2).

Tests assert allow/deny behavior, not sqlglot internals. The allowlist fixture
covers ``myschema.orders``/``myschema.customers`` tables and a small approved
function set; the DWS-compat corpus (testing decision 2) is the same table.
"""

from __future__ import annotations

import pytest

from tau_coding.dataquery.policy import AllowedObjects, SqlPolicyChecker

ALLOWED = AllowedObjects.parse(["myschema.orders", "myschema.customers"])


def checker(
    *,
    allowed: AllowedObjects = ALLOWED,
    require_schema_qualified: bool = True,
) -> SqlPolicyChecker:
    return SqlPolicyChecker(
        allowed_objects=allowed,
        require_schema_qualified_tables=require_schema_qualified,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM myschema.orders",
        "SELECT id, region, amount FROM myschema.orders",
        "SELECT * FROM myschema.orders WHERE region = %s AND amount > %s",
        "SELECT region, SUM(amount) AS total FROM myschema.orders GROUP BY region",
        (
            "SELECT region, COUNT(*) AS n FROM myschema.orders GROUP BY region "
            "ORDER BY n DESC LIMIT 5"
        ),
        (
            "SELECT o.id, c.name FROM myschema.orders o JOIN myschema.customers c "
            "ON o.customer_id = c.id"
        ),
        (
            "SELECT * FROM myschema.orders o WHERE EXISTS (SELECT 1 FROM "
            "myschema.customers c WHERE c.id = o.customer_id)"
        ),
        "SELECT ROW_NUMBER() OVER (ORDER BY amount DESC) AS rank FROM myschema.orders",
        "SELECT COALESCE(region, 'unknown') FROM myschema.orders",
        "SELECT DATE_TRUNC('day', created_at) FROM myschema.orders",
        "SELECT CAST(amount AS text) FROM myschema.orders",
        "SELECT a FROM myschema.orders UNION SELECT a FROM myschema.customers",
        "SELECT * FROM myschema.orders WHERE region = %s -- trailing comment",
        "SELECT /* leading comment */ * FROM myschema.orders",
        "WITH recent AS (SELECT * FROM myschema.orders WHERE amount > %s) SELECT * FROM recent",
        "SELECT * FROM myschema.orders WHERE note = '100% done' AND region = %s",
        # system schema (metadata) queries are always allowed
        "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'exchange_service'",
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s",
    ],
)
def test_policy_allows_read_only_queries(sql: str) -> None:
    report = checker().validate(sql)
    assert report.allowed, report.reason


@pytest.mark.parametrize(
    "sql",
    [
        "",  # empty
        "   ",  # blank
        "SELECT 1; SELECT 2",  # multi-statement
        "UPDATE myschema.orders SET region = 'east'",  # DML
        "DELETE FROM myschema.orders",  # DML
        "INSERT INTO myschema.orders VALUES (1)",  # DML
        (
            "MERGE INTO myschema.orders USING myschema.customers ON (1=1) WHEN MATCHED THEN DELETE"
        ),  # DML
        "CREATE TABLE t (a int)",  # DDL
        "DROP TABLE myschema.orders",  # DDL
        "ALTER TABLE myschema.orders ADD COLUMN x int",  # DDL
        "TRUNCATE TABLE myschema.orders",  # DDL
        "COPY myschema.orders TO STDOUT",  # copy
        "SET statement_timeout = 1000",  # set
        "SHOW search_path",  # command
        "LOCK TABLE myschema.orders IN ACCESS SHARE MODE",  # lock
        "EXPLAIN SELECT * FROM myschema.orders",  # explain
        "SELECT * INTO myschema.new_table FROM myschema.orders",  # SELECT INTO
        "SELECT * FROM myschema.orders FOR UPDATE",  # locking clause
        "SELECT * FROM myschema.orders FOR SHARE",  # locking clause
        "WITH x AS (DELETE FROM myschema.orders RETURNING *) SELECT * FROM x",  # modifying CTE
        (
            "WITH x AS (UPDATE myschema.orders SET region='e' RETURNING *) SELECT * FROM x"
        ),  # modifying CTE
        (
            "WITH x AS (INSERT INTO myschema.orders VALUES (1) RETURNING *) SELECT * FROM x"
        ),  # modifying CTE
        "SELECT pg_sleep(1)",  # dangerous function
        "SELECT pg_read_file('/etc/passwd')",  # dangerous function
        "SELECT lo_export(a, b)",  # dangerous function
        "SELECT dblink_exec('x')",  # dangerous function
        "SELECT my_udf(a) FROM myschema.orders",  # unknown function
        "SELECT pg_catalog.count(*) FROM myschema.orders",  # unapproved schema-qualified function
        "SELECT myschema.custom_fn(a) FROM myschema.orders",  # schema-qualified unknown
        "SELECT * FROM orders",  # unqualified table
        "SELECT * FROM public.orders",  # table not in allowlist
        "SELECT * FROM myschema.orders2",  # table not in allowlist
        "VALUES (1)",  # no table
        "TABLE myschema.orders",  # table statement
    ],
)
def test_policy_rejects_non_read_only_or_unsafe_sql(sql: str) -> None:
    report = checker().validate(sql)
    assert not report.allowed, f"expected rejection: {sql!r}"


def test_policy_rejects_garbage_that_does_not_parse() -> None:
    assert not checker().validate("SELEC * FRM myschema.orders").allowed
    assert not checker().validate("SELECT * FROM myschema.orders WHERE").allowed
    assert not checker().validate(";\n;").allowed


def test_policy_rejects_schema_qualified_function_even_when_name_allowed() -> None:
    # pg_catalog.count is a builtin, but schema-qualified calls are only
    # allowed when explicitly approved (decision 10).
    report = checker().validate("SELECT pg_catalog.count(*) FROM myschema.orders")
    assert not report.allowed
    assert "schema-qualified" in (report.reason or "")


def test_policy_allows_explicitly_approved_schema_qualified_function() -> None:
    check = SqlPolicyChecker(
        allowed_objects=ALLOWED,
        approved_functions=["COUNT", "PG_CATALOG.COUNT"],
    )
    report = check.validate("SELECT pg_catalog.count(*) FROM myschema.orders")
    assert report.allowed, report.reason


def test_policy_allows_unqualified_tables_when_not_required() -> None:
    # Strict mode requires schema-qualified references even for allowed tables.
    strict = checker()
    assert not strict.validate("SELECT * FROM orders").allowed

    # Lenient mode permits unqualified names that match the allowlist.
    lenient = SqlPolicyChecker(
        allowed_objects=AllowedObjects.parse(["myschema.orders"]),
        require_schema_qualified_tables=False,
    )
    assert lenient.validate("SELECT * FROM orders").allowed
    assert not lenient.validate("SELECT * FROM other_table").allowed

    # A wildcarded schema also satisfies an unqualified reference.
    wildcard = SqlPolicyChecker(
        allowed_objects=AllowedObjects.parse(["public"]),
        require_schema_qualified_tables=False,
    )
    assert wildcard.validate("SELECT * FROM orders").allowed


def test_policy_empty_allowlist_rejects_every_table() -> None:
    check = SqlPolicyChecker()
    assert not check.validate("SELECT * FROM myschema.orders").allowed


def test_policy_schema_wildcard_allows_any_table_in_schema() -> None:
    check = SqlPolicyChecker(allowed_objects=AllowedObjects.parse(["myschema"]))
    assert check.validate("SELECT * FROM myschema.any_table").allowed
    assert not check.validate("SELECT * FROM other.any_table").allowed


def test_policy_rejects_nested_modifying_statement_in_subquery() -> None:
    sql = (
        "SELECT * FROM myschema.orders WHERE id IN (SELECT id FROM "
        "(DELETE FROM myschema.customers RETURNING id) x)"
    )
    assert not checker().validate(sql).allowed


def test_policy_string_literal_does_not_trigger_function_rules() -> None:
    report = checker().validate("SELECT note FROM myschema.orders WHERE note LIKE %s")
    assert report.allowed, report.reason


def test_allowed_objects_parse_rejects_invalid_entries() -> None:
    from tau_coding.dataquery.config import DataQueryConfigError

    with pytest.raises(DataQueryConfigError):
        AllowedObjects.parse(["a.b.c"])
