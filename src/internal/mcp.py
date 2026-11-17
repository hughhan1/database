from typing import TYPE_CHECKING, Any, override

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context
    from mcp_types import CallToolResult, InputRequiredResult


class DatabaseServer(MCPServer[None]):
    @override
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[None, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        if name == "execute" and arguments.keys() - {"statements"}:
            raise ToolError("execute accepts only the statements argument.")
        return await super().call_tool(name, arguments, context)
