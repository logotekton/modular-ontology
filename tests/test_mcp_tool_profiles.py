from __future__ import annotations

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from modular_ontology import mcp_server


def _listed_names(profile: str) -> set[str]:
    async def collect() -> set[str]:
        mcp_server.mcp.set_tool_profile(profile)
        return {tool.name for tool in await mcp_server.mcp.list_tools()}

    return anyio.run(collect)


def test_tool_profiles_expose_only_canonical_tools() -> None:
    original_profile = mcp_server.mcp.tool_profile
    try:
        registered = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
        core = _listed_names("core")
        expert = _listed_names("expert")

        assert core == set(mcp_server.CORE_TOOL_NAMES)
        assert expert == set(mcp_server.TOOL_NAMES)
        assert expert == registered
        assert len(core) == 12
        assert len(expert) == 59
        assert "list_projects" not in core
        assert "list_projects" not in expert
        assert set(mcp_server._REMOVED_LEGACY_TOOL_NAMES).isdisjoint(registered)
    finally:
        mcp_server.mcp.set_tool_profile(original_profile)


def test_core_profile_rejects_hidden_tool_calls() -> None:
    original_profile = mcp_server.mcp.tool_profile

    async def call_hidden_tool() -> None:
        mcp_server.mcp.set_tool_profile("core")
        with pytest.raises(ToolError, match="not exposed"):
            await mcp_server.mcp.call_tool("list_projects", {})

    try:
        anyio.run(call_hidden_tool)
    finally:
        mcp_server.mcp.set_tool_profile(original_profile)


def test_tool_profile_aliases_and_invalid_values() -> None:
    assert mcp_server._normalize_tool_profile("canonical") == "expert"
    for removed_profile in ("legacy", "full", "all", "unknown"):
        with pytest.raises(ValueError, match="Unknown MCP tool profile"):
            mcp_server._normalize_tool_profile(removed_profile)
