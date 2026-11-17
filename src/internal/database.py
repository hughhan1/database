"""PostgreSQL store isolation and SQL execution."""

import asyncio
import base64
import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import cache
from hashlib import sha256
from itertools import pairwise
from typing import Any, Final, cast

import certifi
from anyio import CancelScope
from holon.context import get_context
from mcp.server.mcpserver.exceptions import ToolError
from pglast import Error as ParseError
from pglast import ast, parse_sql
from pglast.parser import scan
from psycopg import (
    AsyncConnection as DriverConnection,
)
from psycopg import (
    AsyncRawCursor,
)
from psycopg import (
    Error as DriverError,
)
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import URL, make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

ROWS: Final = 10_000
BYTES: Final = 4 * 1024 * 1024
# What each call made with a key returned, so a repeat returns it again.
KEYS: Final = """
CREATE TABLE _holon.holon_keys (
    key text PRIMARY KEY,
    results jsonb,
    at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX holon_keys_at ON _holon.holon_keys (at)"""

type Value = str | int | float | bool | list[Value] | None


class Statement(BaseModel):
    sql: str  # named parameters, such as :id
    values: dict[str, Value] = {}
    expect: int | None = None  # rows it must change; any other undoes the call


class Result(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    count: int  # rows returned or changed


RESULTS: Final = TypeAdapter(list[Result])


def store() -> str:
    """The calling app's store, which names both its database and its role.

    Nothing a caller sends can name another.
    """
    caller = get_context().caller
    if caller is None:
        raise ToolError(
            "Only apps can use the database. Call the app that owns the data."
        )
    digest = sha256(f"{caller.session}/{caller.app}".encode()).hexdigest()[:20]
    return f"{caller.app[:30]}_{digest}"


@cache
def admin() -> URL:
    """The only credential: the admin's login, from the installation's three values.

    Read on first use, since Holon reads the app's tools with no values set.
    """
    if plain := os.environ.get(
        "DATABASE_URL"
    ):  # the tests' local Postgres, without TLS
        url = make_url(plain).set(drivername="postgresql+psycopg")
    else:
        url = URL.create(
            "postgresql+psycopg",
            username=os.environ["DATABASE_USER"],
            password=os.environ["DATABASE_PASSWORD"],
            host=os.environ["DATABASE_HOST"],
            port=5432,
            database="postgres",
            query={
                "sslmode": "verify-full",
                "sslrootcert": certifi.where(),
            },
        )
    if not url.password:
        raise RuntimeError("The admin needs a password: each store's derives from it")
    return url


def password(store: str) -> str:
    """A store's password, derived from the admin's, so the provider stores nothing."""
    key = str(admin().password).encode()
    return hmac.new(key, store.encode(), "sha256").hexdigest()


@cache
def engine(store: str) -> AsyncEngine:
    """Connections as one store's role, pooled by SQLAlchemy."""
    _, dot, branch = str(admin().username).partition(".")
    url = admin().set(
        username=f"{store}{dot}{branch}", password=password(store), database=store
    )
    return create_async_engine(url, pool_pre_ping=True)


async def create(store: str) -> None:
    """Create the store's role and database as the admin, or set its password again.

    Safe to repeat, and replicas that miss the same store at once take turns.
    """
    setup = create_async_engine(
        admin(), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    async with setup.connect() as connection:
        await connection.exec_driver_sql(
            "SELECT pg_advisory_lock(hashtext(%(store)s))", {"store": store}
        )
        can_use_role = (
            await connection.exec_driver_sql(
                "SELECT pg_has_role(oid, 'SET') AND pg_has_role(oid, 'USAGE') "
                "FROM pg_roles WHERE rolname = %(store)s",
                {"store": store},
            )
        ).scalar_one_or_none()
        name = connection.dialect.identifier_preparer.quote_identifier(store)
        if can_use_role is None:
            await connection.exec_driver_sql(f"CREATE ROLE {name} NOLOGIN")
        if not can_use_role:
            await connection.exec_driver_sql(
                f"GRANT {name} TO CURRENT_USER WITH SET TRUE, INHERIT TRUE"
            )
        database = await connection.exec_driver_sql(
            "SELECT 1 FROM pg_database WHERE datname = %(store)s", {"store": store}
        )
        if not database.first():
            await connection.exec_driver_sql(f"CREATE DATABASE {name} OWNER {name}")
        else:
            await connection.exec_driver_sql(f"ALTER DATABASE {name} OWNER TO {name}")
        await connection.exec_driver_sql(
            f"REVOKE CONNECT ON DATABASE {name} FROM PUBLIC"
        )
        metadata = create_async_engine(admin().set(database=store), poolclass=NullPool)
        try:
            async with metadata.begin() as own:
                await own.exec_driver_sql("SET LOCAL search_path = pg_catalog")
                await own.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS _holon")
                owned = await own.exec_driver_sql(
                    "SELECT nspowner = current_user::regrole FROM pg_namespace "
                    "WHERE nspname = '_holon'"
                )
                if not owned.scalar_one():
                    raise ToolError(
                        "The _holon schema is reserved for the database provider."
                    )
                existing = await own.exec_driver_sql(
                    "SELECT to_regclass('_holon.holon_keys') AS current, "
                    "to_regclass('public.holon_keys') AS legacy"
                )
                tables = existing.mappings().one()
                if tables["current"] is None:
                    if tables["legacy"] is not None:
                        await own.exec_driver_sql(
                            "ALTER TABLE public.holon_keys SET SCHEMA _holon"
                        )
                    else:
                        await own.exec_driver_sql(KEYS)
                await own.exec_driver_sql(f"GRANT USAGE ON SCHEMA _holon TO {name}")
                await own.exec_driver_sql(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON _holon.holon_keys "
                    f"TO {name}"
                )
        finally:
            await metadata.dispose()
        await connection.exec_driver_sql(
            f"ALTER ROLE {name} LOGIN PASSWORD '{password(store)}'"
        )


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    """One transaction in the calling app's store, as its own role."""
    name = store()
    try:
        connection = await engine(name).connect()
    except OperationalError:  # no such store yet, or the admin password changed
        await create(name)
        connection = await engine(name).connect()
    try:
        async with connection.begin():
            await connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
            yield connection
    finally:
        with CancelScope(shield=True):
            await release(connection)


async def release(connection: AsyncConnection) -> None:
    """A pooled connection must not carry session state into another call."""
    try:
        await connection.rollback()
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        raw = await connection.get_raw_connection()
        driver = cast("DriverConnection[Any]", raw.driver_connection)
        async with AsyncRawCursor(driver) as cursor:
            await cursor.execute("DISCARD ALL", prepare=True)
    except DBAPIError, DriverError:
        await connection.invalidate()
    finally:
        await connection.close()


def plain(value: object) -> object:  # noqa: PLR0911  # a return per kind of value
    """A Postgres value as JSON."""
    match value:
        case None | bool() | int() | float() | str() | dict():
            return value
        case Decimal() if value.is_finite() and value == value.to_integral_value():
            return int(value)
        case Decimal():
            return float(value)
        case list():
            return [plain(each) for each in value]
        case datetime() | date() | time():
            return value.isoformat()
        case timedelta():
            return value.total_seconds()
        case bytes():
            return base64.b64encode(value).decode()
        case _:
            return str(value)


def failure(error: DBAPIError | DriverError) -> str:
    """Postgres's message and code."""
    original = error.orig if isinstance(error, DBAPIError) else error
    code = getattr(original, "sqlstate", None)
    return f"failed with SQLSTATE {code}: {original}" if code else f"failed: {original}"


def compile_statement(index: int, statement: Statement) -> tuple[str, list[Value]]:
    """Bind only SQL tokens, then leave parsing and values to PostgreSQL."""
    sql, names = statement.sql, []
    try:
        tokens = scan(sql)
        replacements = []
        for colon, token in pairwise(tokens):
            if colon.name != "ASCII_58" or colon.end + 1 != token.start:
                continue
            name = sql[token.start : token.end + 1]
            if name not in statement.values:
                continue
            if name not in names:
                names.append(name)
            replacements.append(
                (colon.start, token.end + 1, f"${names.index(name) + 1}")
            )
        for start, end, value in reversed(replacements):
            sql = sql[:start] + value + sql[end:]
        parsed = parse_sql(sql)
    except ParseError as error:
        raise ToolError(
            f"statements[{index}] failed with SQLSTATE 42601: {error}"
        ) from error
    if len(parsed) != 1:
        raise ToolError(f"statements[{index}] must contain exactly one SQL statement.")
    if isinstance(parsed[0].stmt, ast.TransactionStmt):
        raise ToolError(
            f"statements[{index}] cannot control the transaction; execute owns it."
        )
    return sql, [statement.values[name] for name in names]


async def run(
    connection: AsyncConnection,
    index: int,
    statement: Statement,
    compiled: tuple[str, list[Value]],
) -> Result:
    sql, values = compiled

    async def query() -> tuple[list[str], list[list[Any]], int]:
        raw = await connection.get_raw_connection()
        driver = cast("DriverConnection[Any]", raw.driver_connection)
        async with asyncio.timeout(30), AsyncRawCursor(driver) as cursor:
            await cursor.execute(sql, values, prepare=True)
            columns = [column.name for column in cursor.description or []]
            rows = (
                [
                    [plain(value) for value in row]
                    for row in await cursor.fetchmany(ROWS + 1)
                ]
                if cursor.description
                else []
            )
            count = len(rows) if cursor.description else max(cursor.rowcount, 0)
            return columns, rows, count

    try:
        await connection.exec_driver_sql("SET LOCAL standard_conforming_strings = on")
        await connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
        task = asyncio.create_task(query())
        try:
            columns, rows, count = await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with CancelScope(shield=True), suppress(asyncio.CancelledError):
                await task
            raise
    except (DBAPIError, DriverError) as error:
        raise ToolError(f"statements[{index}] {failure(error)}") from error
    except TimeoutError as error:
        raise ToolError(f"statements[{index}] exceeded 30 seconds.") from error
    if len(rows) > ROWS or len(json.dumps(rows).encode()) > BYTES:
        raise ToolError(
            f"statements[{index}] returned more than {ROWS:,} rows or 4 MiB. "
            "Select fewer, such as with LIMIT."
        )
    if statement.expect is not None and count != statement.expect:
        raise ToolError(
            f"statements[{index}] expected {statement.expect} rows and got {count}, "
            "so nothing in this call was applied."
        )
    return Result(columns=columns, rows=rows, count=count)


async def first(connection: AsyncConnection, key: str) -> list[Result] | None:
    """What the call that first used key returned, or None once this call holds it.

    A call that repeats a key still running waits for it, then returns what it did.
    """
    present = await connection.exec_driver_sql(
        "SELECT to_regclass('_holon.holon_keys')"
    )
    if present.scalar_one() is None:
        await create(store())
    claimed = await connection.exec_driver_sql(
        "INSERT INTO _holon.holon_keys (key) VALUES (%(key)s) "
        "ON CONFLICT (key) DO UPDATE "
        "SET at = now() WHERE holon_keys.at < now() - interval '24 hours'",
        {"key": key},
    )
    if claimed.rowcount:
        return None
    earlier = await connection.exec_driver_sql(
        "SELECT results FROM _holon.holon_keys WHERE key = %(key)s", {"key": key}
    )
    return RESULTS.validate_python(earlier.scalar_one())


async def remember(
    connection: AsyncConnection, key: str, results: list[Result]
) -> None:
    """Keep what this call returned under its key, and forget keys a day old."""
    await connection.exec_driver_sql(
        "WITH expired AS (DELETE FROM _holon.holon_keys "
        "WHERE at < now() - interval '24 hours') "
        "UPDATE _holon.holon_keys SET results = %(results)s WHERE key = %(key)s",
        {"results": RESULTS.dump_json(results).decode(), "key": key},
    )


async def execute_batch(statements: list[Statement], key: str | None) -> list[Result]:
    try:
        async with transaction() as connection:
            if (
                key is not None
                and (earlier := await first(connection, key)) is not None
            ):
                return earlier
            compiled = [compile_statement(i, each) for i, each in enumerate(statements)]
            results = [
                await run(connection, i, each, bound)
                for i, (each, bound) in enumerate(
                    zip(statements, compiled, strict=True)
                )
            ]
            if key is not None:
                await remember(connection, key, results)
            return results
    except DBAPIError as error:
        raise ToolError(f"The call {failure(error)}") from error
