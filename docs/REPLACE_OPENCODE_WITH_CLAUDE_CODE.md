# Replace OpenCode with Claude Code CLI

## Summary

Replace OpenCode with Claude Code CLI as the agent runtime, and switch from
`ANTHROPIC_API_KEY` authentication to Claude OAuth. Uses the **Claude Agent SDK**
(`claude-agent-sdk`) for programmatic control and **FastMCP** for custom tool
exposure, eliminating manual subprocess management and stream parsing.

## Motivation

- `cheforg/background-agent` uses Claude Code + OAuth (access/refresh tokens
  stored on Modal Volume)
- `jjbean89/background-agents` uses OpenCode + API keys (`ANTHROPIC_API_KEY` via
  Modal Secret)
- Unifying on Claude OAuth eliminates API key management, enables auto token
  refresh, and uses Claude Pro/Team/Enterprise subscriptions instead of
  pay-per-token API billing

## Feature Changes

### Removed

| Feature | Detail |
| --- | --- |
| OpenAI/Codex model support | No more `provider: "openai"` or `OPENAI_OAUTH_REFRESH_TOKEN`. Claude Code only speaks to Claude models. |
| OpenCode CLI & server | `opencode serve --port 4096` replaced entirely by Claude Agent SDK |
| OpenCode plugin system | `.opencode/tool/` directory and `@opencode-ai/plugin` package no longer used |
| `ANTHROPIC_API_KEY` auth path | Replaced by OAuth credentials. The `llm-api-keys` Modal secret is no longer needed for Claude auth. |
| SSE-based prompt streaming | OpenCode's HTTP SSE stream replaced by `claude_agent_sdk.query()` async iterator |
| `codex-auth-plugin.ts` | OpenAI token proxy plugin removed |
| Manual subprocess JSON parsing | Agent SDK handles streaming, session management, and process lifecycle |

### Changed

| Feature | From | To |
| --- | --- | --- |
| Agent runtime | OpenCode (persistent HTTP server on port 4096) | Claude Agent SDK (`query()` async iterator) backed by Claude Code CLI |
| Auth mechanism | `ANTHROPIC_API_KEY` env var via Modal Secret | OAuth `.credentials.json` on Modal Volume mounted at `/root/.claude` |
| Prompt execution | HTTP POST to `/api/v1/sessions/{id}/prompt` -> SSE stream | `claude_agent_sdk.query(prompt, options)` -> `AsyncIterator[Message]` |
| Custom tools | OpenCode plugins (spawn-task.js, get-task-status.js, cancel-task.js) | FastMCP server exposing the same tools, connected via `ClaudeAgentOptions.mcp_servers` |
| Bridge <-> agent protocol | HTTP/SSE client (~800 lines) | SDK message iteration + thin event mapping (~150 lines) |
| Session persistence | OpenCode session ID at `/tmp/opencode-session-id` | Managed by Agent SDK; `ClaudeSDKClient` maintains context across calls |
| Image packages | `opencode-ai`, `@opencode-ai/plugin`, `zod` (npm) | `@anthropic-ai/claude-code` (npm), `claude-agent-sdk`, `fastmcp` (pip) |
| Default model string | `"anthropic/claude-haiku-4-5"` (provider/model format) | `"claude-sonnet-4-6"` (Claude Code native format) |
| MCP tool server | N/A (OpenCode plugins) | FastMCP Python server (~40 lines for 3 tools) |

### Unchanged

| Feature | Why |
| --- | --- |
| Control plane (Durable Objects, D1, WebSocket protocol) | Bridge maps events to the same format — CP is runtime-agnostic |
| GitHub App auth for git clone/push | Runtime-agnostic |
| Event types & ACK protocol (`token`, `tool_call`, `execution_complete`, etc.) | Bridge still emits the same event types |
| Snapshot/restore | Claude Code session data lives on filesystem, survives snapshots |
| Code-server | Independent process |
| Web/Slack/Extension clients | Consume control plane events, not agent events directly |
| Git sync, hooks, process monitoring | 81% of entrypoint.py is generic infrastructure |
| Push command handling | Git operations in bridge are runtime-agnostic |
| Child session spawning (CP architecture) | Same CP infra, new MCP tool implementation |

---

## Execution Plan

### Phase 0: Setup & Branch

**Goal:** Working development environment on feature branch.

- Create `claude/unify-oauth-agents-RdGwa` branch on both repos
- Clone both repos locally for reference
- Annotate exact boundaries between OpenCode-specific and generic code in
  `bridge.py` (~800 lines OpenCode-specific out of ~1,748) and `entrypoint.py`
  (~150 lines OpenCode-specific out of ~982)

### Phase 1: OAuth Credential Pipeline

**Goal:** Claude OAuth tokens available inside Modal sandboxes.

**Files modified:**

- `packages/modal-infra/src/app.py` — Add
  `claude_auth_volume = Volume.from_name("claude-auth-vol", create_if_missing=True)`
- `packages/modal-infra/src/sandbox/manager.py` — Mount volume at `/root/.claude`
  in `create_sandbox()` and `restore_from_snapshot()`; remove `llm_secrets` from
  sandbox secrets list

**New files:**

- `scripts/setup_claude_oauth.py` — Port credential extraction from
  cheforg/background-agent's setup.py (reads from macOS Keychain or
  `~/.claude/.credentials.json`, uploads to Modal Volume)

**Validation:** Run setup script -> verify `.credentials.json` appears on the
Modal Volume.

**Depends on:** Phase 0

### Phase 2: Swap Image Runtime

**Goal:** Sandbox image ships Claude Code + Agent SDK instead of OpenCode.

**Files modified:**

- `packages/modal-infra/src/images/base.py`:
  - Remove: `npm install -g opencode-ai@latest @opencode-ai/plugin@latest zod`
  - Add: `npm install -g @anthropic-ai/claude-code`
  - Add: `pip install claude-agent-sdk fastmcp`
  - Bump `CACHE_BUSTER`

**Validation:** `modal shell` into a test sandbox -> `claude --version` succeeds,
`python -c "import claude_agent_sdk"` succeeds.

**Depends on:** Phase 0

### Phase 3: Rewrite Entrypoint

**Goal:** Supervisor manages Claude Code environment instead of OpenCode server.

**Files modified:**

- `packages/sandbox-runtime/src/sandbox_runtime/entrypoint.py`

**Remove (~150 lines):**

- `start_opencode()` (lines 387-453) — OpenCode server startup
- `_wait_for_health()` — HTTP health check polling
- `_forward_opencode_logs()` — OpenCode stdout forwarding
- `_setup_openai_oauth()` (lines 310-345) — OpenAI OAuth credential writing
- `_install_tools()` (lines 271-308) — OpenCode plugin deployment to
  `.opencode/tool/`

**Add:**

- `_setup_claude_auth()` — Copy/validate `.credentials.json` from volume mount;
  write Claude Code config to `/root/.claude/settings.json`

**Modify:**

- `run()` — Remove OpenCode server startup from boot sequence; bridge starts
  immediately after git sync + hooks (no health check gate needed since there is
  no HTTP server to wait for)
- `monitor_processes()` — Remove OpenCode from monitored process list; only
  monitor bridge + code-server
- `shutdown()` — Remove OpenCode termination step

**Keep untouched:** `perform_git_sync()`, `_clone_repo()`,
`_update_existing_repo()`, `run_setup_script()`, `run_start_script()`,
`_run_hook()`, boot mode detection, signal handling, `_report_fatal_error()`

**Validation:** Supervisor boots cleanly, skips OpenCode, starts bridge process.

**Depends on:** Phase 1, Phase 2

### Phase 4: Rewrite Bridge Agent Interaction

**Goal:** Bridge uses Claude Agent SDK instead of OpenCode HTTP API.

This was the largest change in the original plan (~800 lines replaced with ~400).
Using the Agent SDK reduces this to ~150 lines of event mapping code.

#### Phase 4a: Remove OpenCode HTTP/SSE Layer

| Method to remove | Approx lines | What it did |
| --- | --- | --- |
| `_create_opencode_session()` | 668-687 | POST `/session` to create OpenCode session |
| `_stream_opencode_response_sse()` | 870-1284 | POST prompt + parse SSE response stream |
| `_parse_sse_stream()` | 825-868 | SSE chunk accumulation and JSON parsing |
| `_fetch_final_message_state()` | 1285-1375 | GET `/session/{id}` for missed content after SSE ends |
| `_build_prompt_request_body()` | 700-753 | Construct OpenCode prompt HTTP request body |
| `_extract_error_message()` | 598-604 | Parse OpenCode error response objects |

Also remove `http_client` (httpx `AsyncClient`) from `__init__`, the ascending ID
generator (lines 41-97), and the Anthropic/OpenAI extended thinking format
builders (lines 755-822).

#### Phase 4b: Add Agent SDK Integration

Replace the entire OpenCode interaction layer with the Agent SDK:

```python
from claude_agent_sdk import query, ClaudeAgentOptions

async def _run_prompt(self, prompt: str, message_id: str):
    options = ClaudeAgentOptions(
        model=self.model,
        max_turns=50,
        permission_mode="bypassPermissions",
        allowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep",
                        "WebFetch", "WebSearch"],
        mcp_servers={
            "tasks": {
                "type": "stdio",
                "command": "python",
                "args": ["/app/sandbox_runtime/mcp_tools.py"],
            }
        },
        cwd=self.workdir,
    )

    try:
        async for message in query(prompt=prompt, options=options):
            event = self._map_sdk_message(message, message_id)
            if event:
                await self._send_event(event)

        await self._send_event(self._make_execution_complete(message_id))
    except Exception as e:
        await self._send_event(self._make_error_event(message_id, str(e)))
```

**What the SDK handles (no custom code needed):**

- Subprocess spawning and lifecycle of the `claude` binary
- `--output-format stream-json` parsing and typed message delivery
- Session persistence and `--resume` semantics via `ClaudeSDKClient`
- Stream format edge cases (ping, system messages, partial tool calls)
- Graceful cancellation on task abort

#### Phase 4c: Add Event Mapping Layer

Thin mapping from SDK message types to control plane events:

| SDK Message Type | Control Plane Event |
| --- | --- |
| `AssistantMessage` (text content) | `token` |
| `AssistantMessage` (tool_use content) | `tool_call` (status: `"running"`) |
| `ToolResultMessage` | `tool_call` (status: `"result"`) + `tool_result` |
| `ResultMessage` | `execution_complete` (critical, with ackId) |
| `ErrorMessage` / exception | `error` (critical, with ackId) |
| Message boundaries (new assistant turn) | `step_start` / `step_finish` |

This is ~100-150 lines of straightforward field mapping, compared to the ~400
lines of stream-json parsing in the original plan.

#### Phase 4d: Update Command Handlers

- `_handle_prompt()` — Call `_run_prompt()` instead of OpenCode HTTP; keep git
  identity config
- `_handle_stop()` — Cancel the SDK query task (`task.cancel()`) instead of HTTP
  abort or SIGINT/SIGTERM
- `_load_session_id()` / `_save_session_id()` — Delegate to SDK session
  management; remove `/tmp/opencode-session-id` file I/O
- Replace `self.opencode_session_id` with SDK client state throughout
- Remove `self.opencode_port` and `self.http_client` from constructor

**Keep untouched:** All WebSocket logic, `run()`, `_connect_and_run()`,
`_heartbeat_loop()`, `_send_event()`, `_buffer_event()`,
`_flush_event_buffer()`, `_flush_pending_acks()`, `_make_ack_id()`,
`_handle_push()`, `_handle_snapshot()`, `_handle_shutdown()`,
`_configure_git_identity()`, reconnection/backoff

**Validation:** Send prompt via control plane WebSocket -> tokens stream to
client -> `execution_complete` fires with ackId -> ACK received.

**Depends on:** Phase 2, Phase 3

### Phase 5: Migrate Custom Tools to FastMCP

**Goal:** `spawn-task`, `get-task-status`, `cancel-task` available to Claude Code
via MCP.

**New file:**

- `packages/sandbox-runtime/src/sandbox_runtime/mcp_tools.py` — Single FastMCP
  server exposing all three tools:

```python
from fastmcp import FastMCP
import httpx
import os

mcp = FastMCP("session-tools")

CP_URL = os.environ["CONTROL_PLANE_URL"]
SESSION_ID = os.environ["SESSION_ID"]
AUTH_TOKEN = os.environ["SANDBOX_AUTH_TOKEN"]
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
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_task_status(task_id: str | None = None) -> dict:
    """Get status of child sessions. Omit task_id to list all."""
    async with httpx.AsyncClient() as client:
        url = f"{BASE}/children/{task_id}" if task_id else f"{BASE}/children"
        resp = await client.get(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def cancel_task(task_id: str) -> dict:
    """Cancel a running child session."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE}/children/{task_id}/cancel", headers=HEADERS
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run()
```

The MCP server is registered in Phase 4b via `ClaudeAgentOptions.mcp_servers` —
no separate config file or process management needed. The Agent SDK starts and
stops the MCP server subprocess automatically.

**Removed files:**

- `packages/sandbox-runtime/src/sandbox_runtime/tools/spawn-task.js`
- `packages/sandbox-runtime/src/sandbox_runtime/tools/get-task-status.js`
- `packages/sandbox-runtime/src/sandbox_runtime/tools/cancel-task.js`
- `packages/sandbox-runtime/src/sandbox_runtime/tools/_bridge-client.js`
- `packages/sandbox-runtime/src/sandbox_runtime/plugins/` (legacy)

**Validation:** Claude Code invokes `spawn-task` -> child session created ->
`get-task-status` returns results.

**Depends on:** Phase 4

### Phase 6: Control Plane Cleanup

**Goal:** Remove OpenCode-specific assumptions, restrict model validation to
Claude.

**Files modified:**

- `packages/control-plane/src/session/schema.ts` — Migration: rename
  `opencode_session_id` -> `agent_session_id` (additive: add new column,
  backfill, drop old column to avoid breaking running sandboxes)
- `packages/control-plane/src/session/message-queue.ts` — Update
  `isValidModel()` for Claude model IDs only; remove `provider/model` prefix
  parsing
- `packages/control-plane/src/session/durable-object.ts` — Remove OpenAI config
  paths; update `DEFAULT_MODEL`
- `packages/control-plane/src/session/types.ts` — Update `PromptCommand.model`
  docs
- `packages/sandbox-runtime/src/sandbox_runtime/types.py` — Remove `provider`
  field from `SessionConfig`; rename `opencode_session_id` ->
  `agent_session_id`
- `packages/modal-infra/.env.example` — Remove `ANTHROPIC_API_KEY`; add OAuth
  setup reference

**Validation:** Create session with `claude-sonnet-4-6` -> prompt flows
end-to-end.

**Depends on:** Phase 4

### Phase 7: Setup Script & Documentation

**Goal:** Users can onboard with Claude OAuth.

**New files:**

- `scripts/setup_claude_oauth.py` — Interactive setup: extract OAuth from local
  machine, upload to Modal Volume, verify
- `docs/CLAUDE_OAUTH.md` — Step-by-step setup guide

**Modified files:**

- `terraform/README.md` — Remove `ANTHROPIC_API_KEY` from required secrets
- `README.md` — Update auth section

**Removed files:**

- `docs/OPENAI_MODELS.md`

**Depends on:** Phase 1, Phase 6

### Phase 8: Integration Testing

| Test | Validates |
| --- | --- |
| Fresh sandbox boot -> prompt -> streamed response | Full pipeline |
| Second prompt in same session | Session persistence via SDK |
| Snapshot -> restore -> prompt continues context | Session survives snapshot |
| Child task spawning via MCP tool | FastMCP tools work |
| Push command from control plane | Git operations unaffected |
| Code-server access in browser | Independent process still works |
| Token expiry -> auto-refresh | OAuth refresh works in sandbox |
| Sandbox timeout -> clean shutdown | Graceful lifecycle |
| Bridge disconnect -> reconnect -> resume | Event buffering + ACK replay |

**Depends on:** All previous phases

---

## Dependency Graph

```
Phase 0 (setup)
  |
  +---> Phase 1 (OAuth pipeline) --+
  |                                 +---> Phase 3 (entrypoint) ---+
  +---> Phase 2 (image swap) ------+                              |
                                                                  |
                                   +---> Phase 4 (bridge/SDK) ---+
                                   |         |                    |
                                   |         +---> Phase 5 (MCP)  |
                                   |         |                    |
                                   |         +---> Phase 6 (CP) --+
                                   |                              |
                                   +--- Phase 1 ---> Phase 7 (docs)
                                                                  |
                                   Phase 8 (tests) <--------------+
```

Phases 1 & 2 in parallel. Phases 3 & 4 in parallel (after 1+2). Phase 5 after
4. Phase 6 after 4. Phase 7 after 1+6. Phase 8 last.

---

## Key Risks

| Risk | Mitigation |
| --- | --- |
| Claude Agent SDK Python API changes between versions | Pin `claude-agent-sdk` version in image; test on upgrade |
| OAuth token expiry during long-running tasks | Claude Code handles refresh automatically; setup script validates token before upload |
| SDK message types don't cover all edge cases | SDK is the official abstraction — edge cases are their bug, not ours. Fall back to raw `ResultMessage` fields if needed. |
| `--resume` session data corrupted after snapshot restore | Test thoroughly in Phase 8; Claude Code stores sessions as JSON files that survive filesystem snapshots |
| Event mapping misses edge cases | Build comprehensive mapping tests; SDK typed messages make this easier to validate than raw JSON |
| FastMCP tool errors not surfaced cleanly | FastMCP `ToolError` maps to MCP error responses; Agent SDK surfaces these as tool result errors |
| Loss of OpenAI model support blocks some users | Document clearly; this is a deliberate tradeoff for OAuth unification |
| D1 schema migration breaks running sandboxes | Use additive migration (add new column, backfill, drop old) instead of rename |

---

## Comparison: Original Plan vs SDK-Based Plan

| Aspect | Original Plan | SDK-Based Plan |
| --- | --- | --- |
| Bridge agent interaction | ~400 lines custom subprocess + stream-json parsing | ~150 lines SDK iteration + event mapping |
| MCP server | Custom server + config generation + process management | ~40 lines FastMCP + auto-managed by SDK |
| Session management | Manual file I/O at `/tmp/claude-session-id` + `--resume` flag | Handled by `ClaudeSDKClient` |
| Stream format risks | Must handle ping, system, partial, error JSON variants | SDK delivers typed `Message` objects |
| Stop/cancel | SIGINT + SIGTERM fallback + timeout | `task.cancel()` on async task |
| Total custom code delta | ~800 removed, ~600 added | ~800 removed, ~200 added |
