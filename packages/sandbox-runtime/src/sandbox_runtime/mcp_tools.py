"""
FastMCP server exposing session management tools to Claude Code.

Provides spawn-task, get-task-status, and cancel-task tools that
communicate with the control plane via HTTP. Connected to Claude Code
via ClaudeAgentOptions.mcp_servers in the bridge.

Environment variables (set by sandbox manager):
  CONTROL_PLANE_URL: Base URL of the control plane
  SESSION_ID: Current session ID (from SESSION_CONFIG)
  SANDBOX_AUTH_TOKEN: Bearer token for control plane auth
"""

import json
import os

import httpx
from fastmcp import FastMCP

mcp = FastMCP("session-tools")

CP_URL = os.environ.get("CONTROL_PLANE_URL", "")
SESSION_CONFIG = json.loads(os.environ.get("SESSION_CONFIG", "{}"))
SESSION_ID = SESSION_CONFIG.get("session_id", "")
AUTH_TOKEN = os.environ.get("SANDBOX_AUTH_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}"}
BASE = f"{CP_URL}/sessions/{SESSION_ID}"


@mcp.tool()
async def spawn_task(title: str, prompt: str, model: str = "claude-sonnet-4-6") -> dict:
    """Spawn a child coding session to work on a subtask in parallel."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE}/children",
            json={"title": title, "prompt": prompt, "model": model},
            headers=HEADERS,
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_task_status(task_id: str | None = None) -> dict:
    """Get status of child sessions. Omit task_id to list all."""
    async with httpx.AsyncClient() as client:
        url = f"{BASE}/children/{task_id}" if task_id else f"{BASE}/children"
        resp = await client.get(url, headers=HEADERS, timeout=30.0)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def cancel_task(task_id: str) -> dict:
    """Cancel a running child session."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE}/children/{task_id}/cancel",
            headers=HEADERS,
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run()
