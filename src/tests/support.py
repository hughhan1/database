"""Apps that call the database provider through its MCP tool."""

from contextvars import ContextVar

from mcp import Client

from src import server

# The in-memory server reads the calling task's context, so each call sets its own.
admitted = ContextVar("admitted", default=None)


async def call(by, statements, provider=None):
    """Calls execute as by, an app, or as a person signed out when by is None."""
    statements = [
        {"sql": each} if isinstance(each, str) else each for each in statements
    ]
    token = admitted.set(by)
    try:
        async with Client(provider or server.server, mode="2026-07-28") as client:
            return await client.call_tool("execute", {"statements": statements})
    finally:
        admitted.reset(token)


async def execute(by, *statements, provider=None):
    """The results of a call that must succeed."""
    result = await call(by, statements, provider)
    assert not result.is_error, result.content
    return result.structured_content["result"]


async def fails(by, *statements):
    """The error of a call that must fail."""
    result = await call(by, statements)
    assert result.is_error, result.structured_content
    return result.content[0].text


async def rows(by, sql):
    [result] = await execute(by, sql)
    return result["rows"]
