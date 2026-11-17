"""A private Postgres store for each app that requires database."""

from typing import Annotated

from pydantic import Field

from src.internal.database import Result, Statement, execute_batch
from src.internal.mcp import DatabaseServer

server = DatabaseServer("io.github.hughhan1/database", version="0.2.0")


@server.tool()
async def execute(
    statements: Annotated[list[Statement], Field(max_length=100)],
) -> list[Result]:
    """Run statements in the calling app's store in one PostgreSQL transaction.

    Each entry contains one SQL statement. Transaction-control commands are refused.
    PostgreSQL rollback rules apply, including possible gaps in sequence values.

    A statement's sql writes :name for values[name], and :name::type casts it. Postgres
    reads a text value as whatever type its place needs, such as a timestamp or jsonb.
    expect is how many rows the statement must return or change; any other count
    undoes the whole call. An error names its statement as statements[index], with
    Postgres's SQLSTATE and message.

    Values come back as JSON: numbers as numbers (numeric as a float unless it is
    whole), json and jsonb as JSON, arrays as lists, timestamps, dates and times as
    ISO 8601, intervals as seconds, uuid as text, bytea as base64, and anything else
    as Postgres's text for it.

    A statement may run 30 seconds and return 10,000 rows or 4 MiB.
    A call may hold up to 100 statements.
    """
    return await execute_batch(statements)
