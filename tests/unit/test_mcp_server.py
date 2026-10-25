"""Exercise the real stdio entry point with modern and legacy MCP clients."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_stdio_discovery_and_read_only_call(tmp_path, mode):
    domains = [{"name": "example.test", "cloud": "aws"}]
    (tmp_path / "data.json").write_text(json.dumps({"domains": domains}))
    source = Path(__file__).resolve().parents[2] / "src"
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "noc_ops.mcp_server"],
        env={**os.environ, "NOC_HOME": str(tmp_path), "PYTHONPATH": str(source)},
    )

    async def check():
        async with Client(server, mode=mode, read_timeout_seconds=10) as client:
            assert client.server_info.name == "multicloud-ops"
            listing = await client.list_tools()
            assert {"query_domains", "list_instances", "reboot_instance"} <= {
                tool.name for tool in listing.tools
            }
            result = await client.call_tool("query_domains", {})
            assert not result.is_error
            assert json.loads(result.content[0].text) == domains

    asyncio.run(asyncio.wait_for(check(), timeout=20))

