import os
from uuid import uuid4

import anyio
import psycopg
import pytest
from mcp import Client
from psycopg import sql

from src import server
from src.internal import database
from src.tests.support import admitted, execute, fails, rows

pytestmark = pytest.mark.anyio

NOTES = ("CREATE TABLE notes (body text)", "INSERT INTO notes VALUES ('kept')")


async def test_an_app_reaches_only_its_own_store(app, test_admin):
    notes, spy = app("notes"), app("spy")
    await execute(notes, *NOTES)
    [[store]] = await rows(notes, "SELECT current_database()")

    assert "SQLSTATE 42P01" in await fails(spy, "SELECT * FROM notes")
    assert "SQLSTATE 42P01" in await fails(spy, "DELETE FROM notes")
    assert "SQLSTATE 0A000" in await fails(spy, f'SELECT * FROM "{store}".public.notes')
    assert "SQLSTATE 42501" in await fails(spy, f'SET ROLE "{store}"')
    await execute(spy, "CREATE TABLE notes (body text)", "DROP TABLE notes")
    [[spy_store, spy_role]] = await rows(spy, "SELECT current_database(), current_user")

    admin, _ = test_admin
    spy_url = admin.set(
        database=spy_store, username=spy_role, password=database.password(spy_store)
    )
    with psycopg.connect(spy_url.render_as_string(hide_password=False)) as connection:
        assert connection.execute("SELECT current_user").fetchone() == (spy_role,)
    with (
        pytest.raises(psycopg.OperationalError, match="permission denied for database"),
        psycopg.connect(
            spy_url.set(database=store).render_as_string(hide_password=False)
        ),
    ):
        pytest.fail("The app's role connected to another app's database.")

    assert await rows(notes, "SELECT body FROM notes") == [["kept"]]


async def test_an_app_has_a_store_in_each_session(app):
    production, development = app("notes"), app("notes", "dev-ada")
    await execute(production, *NOTES)

    assert "SQLSTATE 42P01" in await fails(development, "SELECT * FROM notes")
    [[first]] = await rows(production, "SELECT current_database()")
    [[second]] = await rows(development, "SELECT current_database()")
    assert first != second


async def test_a_person_calling_directly_is_refused():
    message = await fails(None, "SELECT 1")
    assert "Only apps can use the database" in message


async def test_a_store_is_made_on_first_use_and_outlives_the_provider(app, start):
    notes = app("notes")
    [[database, role]] = await rows(notes, "SELECT current_database(), current_user")
    assert database == role
    assert database.startswith("notes_")
    assert await rows(notes, "SELECT to_regnamespace('_holon') IS NULL") == [[True]]
    await execute(notes, *NOTES)

    start()
    assert await rows(notes, "SELECT body FROM notes") == [["kept"]]


async def test_its_tools_can_be_listed_without_its_secret(start, monkeypatch):
    # Holon reads an app's tools by importing it with an empty environment.
    for name in list(os.environ):
        monkeypatch.delenv(name)
    start()
    async with Client(server.server, mode="2026-07-28") as client:
        [tool] = (await client.list_tools()).tools
        assert tool.name == "execute"
        assert set(tool.input_schema["properties"]) == {"statements"}


async def test_execute_rejects_obsolete_arguments_before_writing(app):
    notes = app("notes")
    await execute(notes, "CREATE TABLE notes (body text)")
    token = admitted.set(notes)
    try:
        async with Client(server.server, mode="2026-07-28") as client:
            result = await client.call_tool(
                "execute", {"statements": [{"sql": NOTES[1]}], "key": "old-request"}
            )
    finally:
        admitted.reset(token)
    assert result.is_error
    assert await rows(notes, "SELECT body FROM notes") == []


async def test_a_new_admin_password_gives_each_store_a_new_one(
    app, start, monkeypatch, test_admin
):
    notes = app("notes")
    await execute(notes, *NOTES)

    admin_url, _ = test_admin
    rotated = admin_url.set(password=uuid4().hex)
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(
            sql.SQL("ALTER ROLE CURRENT_USER PASSWORD {}").format(
                sql.Literal(rotated.password)
            )
        )
    monkeypatch.setenv("DATABASE_URL", rotated.render_as_string(hide_password=False))
    start()
    assert await rows(notes, "SELECT body FROM notes") == [["kept"]]


async def test_first_use_recovers_a_database_created_before_setup_finished(
    app, start, test_admin
):
    notes = app("notes")
    token = admitted.set(notes)
    try:
        name = database.store()
    finally:
        admitted.reset(token)

    # Model interruption after the database was created but before it was configured.
    admin_url, _ = test_admin
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        store = sql.Identifier(name)
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(store))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(store))

    await execute(notes, *NOTES)
    start()
    assert await rows(notes, "SELECT body FROM notes") == [["kept"]]
    assert await rows(
        notes,
        "SELECT pg_get_userbyid(datdba) = current_user FROM pg_database "
        "WHERE datname = current_database()",
    ) == [[True]]


async def test_replicas_that_miss_the_same_store_at_once_all_get_it(app, replica):
    notes = app("notes")
    results = []

    async def read(provider):
        results.append(await execute(notes, "SELECT 1", provider=provider))

    async with anyio.create_task_group() as group:
        group.start_soon(read, None)
        group.start_soon(read, replica)
    assert results == [[{"columns": ["?column?"], "rows": [[1]], "count": 1}]] * 2
