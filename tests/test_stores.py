import os
from uuid import uuid4

import anyio
import psycopg
import pytest
from mcp import Client
from psycopg import sql
from psycopg.types.json import Jsonb

import server
from tests.support import admitted, call, execute, fails, rows

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
        database=spy_store, username=spy_role, password=server.password(spy_store)
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
    await execute(notes, *NOTES)

    start()
    assert await rows(notes, "SELECT body FROM notes") == [["kept"]]


async def test_its_tools_can_be_listed_without_its_secret(start, monkeypatch):
    # Holon reads an app's tools by importing it with an empty environment.
    for name in list(os.environ):
        monkeypatch.delenv(name)
    start()
    async with Client(server.server, mode="2026-07-28") as client:
        assert [tool.name for tool in (await client.list_tools()).tools] == ["execute"]


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
        name = server.store()
    finally:
        admitted.reset(token)

    # Model interruption after the database was created but before it was configured.
    admin_url, _ = test_admin
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        store = sql.Identifier(name)
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                store, sql.Literal(server.password(name))
            )
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(store))

    result = await execute(notes, *NOTES, key="create-notes")
    start()
    assert await execute(notes, "SELECT 0", key="create-notes") == result
    assert await rows(notes, "SELECT body FROM notes") == [["kept"]]


@pytest.fixture
def legacy_store(app, test_admin):
    notes = app("notes")
    token = admitted.set(notes)
    try:
        name = server.store()
    finally:
        admitted.reset(token)
    password = server.password(name)

    admin_url, _ = test_admin
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        store = sql.Identifier(name)
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                store, sql.Literal(password)
            )
        )
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(store, store))
        admin.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(store)
        )

    store_url = admin_url.set(username=name, password=password, database=name)
    with psycopg.connect(store_url.render_as_string(hide_password=False)) as store:
        store.execute(
            "CREATE TABLE holon_keys ("
            "key text PRIMARY KEY, results jsonb, "
            "at timestamptz NOT NULL DEFAULT now())"
        )
        store.execute("CREATE INDEX holon_keys_at ON holon_keys (at)")
    return notes, store_url, admin_url.set(database=name)


async def test_key_replay_survives_upgrade_from_the_original_provider(legacy_store):
    notes, store_url, _ = legacy_store
    original_reply = [{"columns": ["number"], "rows": [[42]], "count": 1}]
    with psycopg.connect(store_url.render_as_string(hide_password=False)) as store:
        store.execute(
            "INSERT INTO holon_keys (key, results) VALUES (%s, %s)",
            ("original", Jsonb(original_reply)),
        )

    assert await execute(notes, "SELECT 0", key="original") == original_reply
    assert "SQLSTATE 42501" in await fails(notes, "DROP SCHEMA _holon CASCADE")
    await execute(notes, "DROP SCHEMA public CASCADE", "CREATE SCHEMA public")
    assert await execute(notes, "SELECT 0", key="original") == original_reply


async def test_upgrade_does_not_give_legacy_rules_the_admins_privileges(legacy_store):
    notes, store_url, admin_url = legacy_store
    with psycopg.connect(admin_url.render_as_string(hide_password=False)) as admin:
        admin.execute("CREATE TABLE provider_secret (value text)")
        admin.execute("INSERT INTO provider_secret VALUES ('private')")
    with psycopg.connect(store_url.render_as_string(hide_password=False)) as store:
        store.execute("CREATE TABLE copied (value text)")
        store.execute(
            "CREATE RULE copy_secret AS ON INSERT TO holon_keys DO ALSO "
            "INSERT INTO copied SELECT value FROM provider_secret"
        )

    # The rule makes ON CONFLICT invalid, but migration has already completed.
    assert "SQLSTATE 0A000" in await fails(notes, "SELECT 1", key="migrate")
    response = await call(
        notes, ["INSERT INTO _holon.holon_keys (key) VALUES ('copy')"], None
    )
    assert await rows(notes, "SELECT value FROM copied") == []
    assert response.is_error
    assert "SQLSTATE 42501" in response.content[0].text


async def test_replicas_that_miss_the_same_store_at_once_all_get_it(app, replica):
    notes = app("notes")
    results = []

    async def read(provider):
        results.append(await execute(notes, "SELECT 1", provider=provider))

    async with anyio.create_task_group() as group:
        group.start_soon(read, None)
        group.start_soon(read, replica)
    assert results == [[{"columns": ["?column?"], "rows": [[1]], "count": 1}]] * 2
