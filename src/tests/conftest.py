import importlib
import importlib.util
import os
import sys
from uuid import uuid4

import psycopg
import pytest
from mcp.server.auth.provider import AccessToken
from psycopg import sql
from sqlalchemy import make_url

from src import server
from src.internal import database
from src.tests.support import admitted


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_cluster():
    url = make_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
        )
    )
    if url.host not in {"127.0.0.1", "::1"} or url.query:
        pytest.fail(
            "Use a disposable, loopback PostgreSQL instance for provider tests."
        )
    return url.update_query_dict({"hostaddr": url.host})


@pytest.fixture(autouse=True)
def test_admin(monkeypatch, test_cluster, start):
    """Create a test-owned admin and clean up only its exact role and store names."""
    name = f"test_admin_{uuid4().hex}"
    admin = test_cluster.set(username=name, password=uuid4().hex)
    role = sql.Identifier(name)
    stores = set()
    monkeypatch.setenv("DATABASE_URL", admin.render_as_string(hide_password=False))
    monkeypatch.setattr("holon.context.get_access_token", admitted.get)
    start()
    with psycopg.connect(
        test_cluster.render_as_string(hide_password=False), autocommit=True
    ) as cluster:
        cluster.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEDB CREATEROLE PASSWORD {}").format(
                role, sql.Literal(admin.password)
            )
        )
        try:
            yield admin, stores
        finally:
            for store_name in sorted(stores):
                store = sql.Identifier(store_name)
                cluster.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(store)
                )
                cluster.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(store))
            cluster.execute(sql.SQL("DROP ROLE {}").format(role))


@pytest.fixture
def start():
    """Restart the provider with the current environment and empty process caches."""

    def start():
        database.admin.cache_clear()
        database.engine.cache_clear()

    return start


@pytest.fixture
def app(test_admin):
    """Give this test private caller identities and track their stores."""
    admin, stores = test_admin

    def app(name, session="production"):
        caller = {"app": name, "session": f"{admin.username}-{session}"}
        identity = AccessToken(
            token="", client_id="", scopes=[], claims={"caller": caller}
        )
        token = admitted.set(identity)
        try:
            stores.add(database.store())
        finally:
            admitted.reset(token)
        return identity

    return app


@pytest.fixture
def replica(monkeypatch):
    """Load a second provider with its own process-local caches."""
    with monkeypatch.context() as modules:
        for name, path in (
            (database.__name__, database.__file__),
            ("database_replica", server.__file__),
        ):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            modules.setitem(sys.modules, name, module)
            spec.loader.exec_module(module)
    return module.server
