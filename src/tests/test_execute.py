from datetime import UTC, datetime

import anyio
import psycopg
import pytest

from src.tests.support import call, execute, fails, rows

pytestmark = pytest.mark.anyio

EVENTS = "CREATE TABLE events (id text PRIMARY KEY, taken int NOT NULL, capacity int)"
BOOKINGS = "CREATE TABLE bookings (id serial, person text)"
BOOK = {"sql": "INSERT INTO bookings (person) VALUES ('ada') RETURNING id"}


async def test_a_call_is_one_transaction(app):
    shop = app("shop")
    results = await execute(
        shop,
        EVENTS,
        {
            "sql": "INSERT INTO events VALUES (:id, 0, :capacity)",
            "values": {"id": "talk", "capacity": 1},
        },
        "SELECT id, taken FROM events",
    )
    assert results == [
        {"columns": [], "rows": [], "count": 0},
        {"columns": [], "rows": [], "count": 1},
        {"columns": ["id", "taken"], "rows": [["talk", 0]], "count": 1},
    ]


async def test_a_failed_expect_undoes_every_statement_in_the_call(app):
    shop = app("shop")
    await execute(shop, EVENTS, "INSERT INTO events VALUES ('talk', 1, 1)")
    message = await fails(
        shop,
        BOOKINGS,
        "INSERT INTO bookings (person) VALUES ('ada')",
        {
            "sql": "UPDATE events SET taken = taken + 1 "
            "WHERE id = 'talk' AND taken < capacity",
            "expect": 1,
        },
    )
    assert "statements[2] expected 1 rows and got 0" in message
    assert "SQLSTATE 42P01" in await fails(shop, "SELECT * FROM bookings")


async def test_a_failed_statement_undoes_the_call_and_says_why(app):
    shop = app("shop")
    await execute(shop, EVENTS, "INSERT INTO events VALUES ('talk', 0, 1)")
    message = await fails(
        shop,
        "UPDATE events SET taken = 1",
        "INSERT INTO events VALUES ('talk', 0, 1)",
    )
    assert "statements[1] failed with SQLSTATE 23505" in message
    assert 'duplicate key value violates unique constraint "events_pkey"' in message
    assert "statements[0] failed with SQLSTATE 42601" in await fails(shop, "SELEC 1")
    assert await rows(shop, "SELECT taken FROM events") == [[0]]


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN",
        "START TRANSACTION",
        "/* finish */ CoMmIt AND CHAIN",
        "END",
        "ROLLBACK",
        "ABORT",
        "PREPARE TRANSACTION 'unexpected'",
        "COMMIT PREPARED 'unexpected'",
        "ROLLBACK PREPARED 'unexpected'",
        "SAVEPOINT unmanaged",
        "INSERT INTO bookings (person) VALUES ('grace'); COMMIT",
    ],
)
async def test_a_statement_cannot_take_control_of_the_call_transaction(app, sql):
    shop = app("shop")
    await execute(shop, BOOKINGS)
    error = await fails(
        shop,
        BOOK,
        sql,
        {"sql": "SELECT 1", "expect": 0},
    )
    assert await rows(shop, "SELECT person FROM bookings") == []
    assert "statements[1]" in error


async def test_procedure_transaction_control_cannot_commit_a_call(app):
    shop = app("shop")
    await execute(
        shop,
        BOOKINGS,
        "CREATE PROCEDURE finish() LANGUAGE plpgsql AS $$ BEGIN COMMIT; END; $$",
    )
    for sql in ["CALL finish()", "DO $$ BEGIN COMMIT; END; $$"]:
        assert "statements[1]" in await fails(shop, BOOK, sql)
        assert await rows(shop, "SELECT person FROM bookings") == []


async def test_sql_session_settings_and_temp_objects_are_local_to_the_call(app):
    shop = app("shop")
    [previous] = await rows(shop, "SHOW application_name")
    results = await execute(
        shop,
        "SET application_name = 'storage-request'",
        "CREATE TEMP TABLE scratch (id integer)",
        "SHOW application_name",
        "SELECT to_regclass('pg_temp.scratch') IS NOT NULL",
    )
    assert results[-2]["rows"] == [["storage-request"]]
    assert results[-1]["rows"] == [[True]]
    assert await rows(shop, "SHOW application_name") == [previous]
    assert await rows(shop, "SELECT to_regclass('pg_temp.scratch')") == [[None]]


async def test_statement_deadline_rolls_back_writes(app):
    shop = app("shop")
    await execute(shop, BOOKINGS)
    with anyio.fail_after(45):
        error = await fails(
            shop,
            BOOK,
            "DO $$ BEGIN PERFORM set_config('statement_timeout', '0', true); "
            "PERFORM pg_sleep(60); END; $$",
        )

    assert "statements[1]" in error
    assert "30 seconds" in error or "SQLSTATE 57014" in error
    assert await rows(shop, "SELECT person FROM bookings") == []
    await execute(shop, BOOK)
    assert await rows(shop, "SELECT person FROM bookings") == [["ada"]]


async def test_cancelled_mcp_call_rolls_back_writes(app, test_admin):
    shop = app("shop")
    await execute(shop, BOOKINGS)
    [[store]] = await rows(shop, "SELECT current_database()")
    admin, _ = test_admin

    async def book():
        await call(shop, [BOOK, "SELECT pg_advisory_xact_lock(42)"])
        pytest.fail("The blocked call completed before cancellation.")

    async with await psycopg.AsyncConnection.connect(
        admin.set(database=store).render_as_string(hide_password=False),
        autocommit=True,
    ) as connection:
        await connection.execute("SELECT pg_advisory_lock(42)")
        async with anyio.create_task_group() as group:
            group.start_soon(book)
            with anyio.fail_after(10):
                while True:
                    waiting = await connection.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype = 'advisory' "
                        "AND database = (SELECT oid FROM pg_database "
                        "WHERE datname = current_database()) AND NOT granted)"
                    )
                    if await waiting.fetchone() == (True,):
                        break
                    await anyio.sleep(0.01)
            group.cancel_scope.cancel()

        with anyio.fail_after(10):
            assert await rows(shop, "SELECT person FROM bookings") == []
            await execute(shop, BOOK)
    assert await rows(shop, "SELECT person FROM bookings") == [["ada"]]


async def test_values_bind_by_name_and_other_colons_are_sql(app):
    shop = app("shop")
    [result] = await execute(
        shop,
        {
            "sql": """
                SELECT /* café */ :n::int + 1, CAST(:n AS int), '{"k":1}'::jsonb,
                       '12:30'::time, 'a%' LIKE :like, :id = ANY(:ids),
                       ':n; COMMIT' AS ":n", $literal$:n; END$literal$
                       /* nested /* :n */ comment */ -- :n; ROLLBACK
            """,
            "values": {"n": "2", "like": "a%", "id": "b", "ids": ["a", "b"]},
        },
    )
    assert result["columns"][-2] == ":n"
    assert result["rows"] == [
        [3, 2, {"k": 1}, "12:30:00", True, True, ":n; COMMIT", ":n; END"]
    ]


async def test_values_come_back_as_json(app):
    shop = app("shop")
    [result] = await execute(
        shop,
        {
            "sql": "SELECT CAST(:at AS timestamptz), CAST(:day AS date), "
            "1.50::numeric, 2.0::numeric, 9007199254740993::bigint, 0.5::float8, "
            "CAST(:id AS uuid), '\\x6869'::bytea, '{\"a\": [1, 2.5]}'::jsonb, "
            "ARRAY[1, 2], interval '1 hour', '10.0.0.1'::inet, true, NULL",
            "values": {
                "at": "2026-11-12T10:00:00Z",
                "day": "2026-11-12",
                "id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            },
        },
    )
    [[at, *values]] = result["rows"]
    assert datetime.fromisoformat(at) == datetime(2026, 11, 12, 10, tzinfo=UTC)
    assert values == [
        "2026-11-12",
        1.5,
        2,
        9007199254740993,
        0.5,
        "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "aGk=",
        {"a": [1, 2.5]},
        [1, 2],
        3600.0,
        "10.0.0.1",
        True,
        None,
    ]


async def test_the_limits_hold(app):
    shop = app("shop")
    assert await rows(shop, "SHOW statement_timeout") == [["30s"]]
    assert len(await rows(shop, "SELECT generate_series(1, 10000)")) == 10_000
    too_many = await fails(shop, "SELECT generate_series(1, 10001)")
    assert "statements[0] returned more than 10,000 rows or 4 MiB" in too_many
    too_big = await fails(shop, "SELECT repeat('x', 4 * 1024 * 1024)")
    assert "statements[0] returned more than 10,000 rows or 4 MiB" in too_big
    assert len(await execute(shop, *["SELECT 1"] * 100)) == 100
    assert "at most 100" in await fails(shop, *["SELECT 1"] * 101)
