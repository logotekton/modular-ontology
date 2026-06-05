from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("MODULAR_GRAPH_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MODULAR_GRAPH_MCP_PORT", "8011"))
PATH = os.environ.get("MODULAR_GRAPH_MCP_PATH", "/mcp")
URL = os.environ.get("MODULAR_GRAPH_MCP_URL", f"http://{HOST}:{PORT}{PATH}")


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


async def call_remote_mcp(url: str) -> None:
    async with streamablehttp_client(url) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            result = await session.call_tool(
                "ask_pack_question",
                {
                    "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
                    "question": "Beam",
                    "limit": 2,
                },
            )
            payload_text = result.content[0].text if result.content else "{}"
            payload = json.loads(payload_text)
            print(
                json.dumps(
                    {
                        "url": url,
                        "tools": names,
                        "answerMode": payload.get("mode"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )


def wait_until_ready(process: subprocess.Popen[str]) -> None:
    for _ in range(60):
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise RuntimeError(f"MCP server exited early.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
        if port_is_open(HOST, PORT):
            return
        time.sleep(0.25)
    raise TimeoutError(f"MCP server did not open {HOST}:{PORT}")


def main() -> None:
    if os.environ.get("MODULAR_GRAPH_MCP_URL"):
        anyio.run(call_remote_mcp, URL)
        return

    env = os.environ.copy()
    env["MODDULAR_GRAPH_ROOT"] = str(ROOT)
    env.setdefault("MODULAR_GRAPH_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,[::1]:*")
    env.setdefault("MODULAR_GRAPH_MCP_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*,http://[::1]:*")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "moddular_graph.mcp_server",
            "--transport",
            "streamable-http",
            "--host",
            HOST,
            "--port",
            str(PORT),
            "--path",
            PATH,
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_until_ready(process)
        anyio.run(call_remote_mcp, URL)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
