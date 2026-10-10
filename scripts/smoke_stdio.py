"""Smoke-тест: запустить сервер через stdio и вызвать инструменты, как это делает MCP-клиент."""

import shutil
import sys
import tempfile
from pathlib import Path

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main() -> None:
    # Сначала ищем рядом с текущим интерпретатором (venv без activate), потом в PATH
    local = Path(sys.executable).with_name("internship-mcp")
    binary = str(local) if local.exists() else shutil.which("internship-mcp")
    assert binary, "internship-mcp не установлен"
    with tempfile.TemporaryDirectory() as tmp:
        params = StdioServerParameters(
            command=binary, args=["--source", "sample", "--db", f"sqlite:///{tmp}/smoke.db"]
        )
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert "search_internships" in tools, tools
            result = await client.call_tool("search_internships", {"remote_only": True})
            assert not result.is_error, result.content
            found = result.structured_content["total_found"]
            assert found > 0
            print(f"ok: {len(tools)} tools, {found} remote vacancies")


anyio.run(main)
