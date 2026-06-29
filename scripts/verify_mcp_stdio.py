from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "modular_ontology.mcp_server"],
        cwd=ROOT,
        env={"MODULAR_ONTOLOGY_ROOT": str(ROOT)},
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            result = await session.call_tool(
                "mo_question_answer",
                {
                    "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
                    "question": "Beam",
                    "limit": 2,
                },
            )
            payload_text = result.content[0].text if result.content else "{}"
            payload = json.loads(payload_text)
            print(json.dumps({"tools": names, "answerMode": payload.get("mode")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    anyio.run(main)
