"""Validate follow-up identifiers against a successfully executed SQL statement."""

from __future__ import annotations

from collections.abc import Sequence

from sqlglot import exp, parse_one
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema


def validate_reuse_sql(original: str, candidate: str) -> None:
    """Fail closed for new tables, unknown columns, stars or ambiguous references."""
    base = parse_one(original, read="postgres")
    schema: dict[str, dict[str, dict[str, str]]] = {}
    for scope in traverse_scope(base):
        sources = {
            name: source
            for name, (_, source) in scope.selected_sources.items()
            if isinstance(source, exp.Table)
        }
        for column in scope.columns:
            if column.table:
                source = sources.get(column.table)
                if source is None:
                    # Derived sources are checked in their own scopes.
                    if column.table in scope.sources:
                        continue
                    raise ValueError("Unresolved source in original SQL")
            elif len(sources) == 1:
                source = next(iter(sources.values()))
            else:
                raise ValueError("Ambiguous original column; request explicit SAG evidence")
            schema.setdefault(source.db, {}).setdefault(source.name, {})[column.name] = "UNKNOWN"
    query = parse_one(candidate, read="postgres")
    if any(query.find_all(exp.Star)):
        raise ValueError("Follow-up SQL must name its evidenced columns explicitly")
    for scope in traverse_scope(query):
        for _, relation in scope.selected_sources.values():
            if isinstance(relation, exp.Table) and (
                relation.catalog
                or relation.db not in schema
                or relation.name not in schema[relation.db]
            ):
                raise ValueError("Follow-up SQL references a table outside prior evidence")
    qualify(
        query,
        dialect="postgres",
        schema=MappingSchema(dict(schema)),
        infer_schema=False,
        validate_qualify_columns=True,
    )


def validate_reused_metrics(original: str, candidate: str, metrics: Sequence[str]) -> None:
    """A retained metric label/unit must retain its actual expression, not just its columns."""

    def expressions(sql: str) -> dict[str, str]:
        tree = parse_one(sql, read="postgres")
        if not isinstance(tree, exp.Query):
            raise ValueError("Expected a query")
        aliases = {table.alias_or_name: table.name for table in tree.find_all(exp.Table)}
        for column in tree.find_all(exp.Column):
            if column.table in aliases:
                column.set("table", exp.to_identifier(aliases[column.table]))
            elif not column.table and len(aliases) == 1:
                column.set("table", exp.to_identifier(next(iter(aliases.values()))))
        return {
            item.alias_or_name: item.unalias().sql(dialect="postgres", normalize=True)
            for item in tree.selects
        }

    before, after = expressions(original), expressions(candidate)
    if any(name not in before or before[name] != after.get(name) for name in metrics):
        raise ValueError("Changed metric expression requires new SAG evidence")
