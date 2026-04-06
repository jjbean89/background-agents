#!/usr/bin/env python3
"""
Sandbox entrypoint - manages Claude Code agent and bridge lifecycle.

Runs as PID 1 inside the sandbox. Responsibilities:
1. Perform git sync with latest code
2. Run repo hooks (setup/start) based on boot mode
3. Set up Claude Code authentication (OAuth credentials from volume)
4. Start bridge process for control plane communication
5. Monitor processes and restart on crash with exponential backoff
6. Handle graceful shutdown on SIGTERM/SIGINT
"""

import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

from .constants import CODE_SERVER_PORT
from .log_config import configure_logging, get_logger

configure_logging()


class SandboxSupervisor:
    """
    Supervisor process for sandbox lifecycle management.

    Manages:
    - Git synchronization with base branch
    - Claude Code authentication setup
    - Bridge process for control plane communication
    - Process monitoring with crash recovery
    """

    # Configuration
    HEALTH_CHECK_TIMEOUT = 30.0
    MAX_RESTARTS = 5
    BACKOFF_BASE = 2.0
    BACKOFF_MAX = 60.0
    SETUP_SCRIPT_PATH = ".openinspect/setup.sh"
    START_SCRIPT_PATH = ".openinspect/start.sh"
    DEFAULT_SETUP_TIMEOUT_SECONDS = 300
    DEFAULT_START_TIMEOUT_SECONDS = 120
    CLONE_DEPTH_COMMITS = 100

    def __init__(self):
        self.bridge_process: asyncio.subprocess.Process | None = None
        self.code_server_process: asyncio.subprocess.Process | None = None
        self.shutdown_event = asyncio.Event()
        self.git_sync_complete = asyncio.Event()
        self.boot_mode = "unknown"

        # Configuration from environment (set by Modal/SandboxManager)
        self.sandbox_id = os.environ.get("SANDBOX_ID", "unknown")
        self.control_plane_url = os.environ.get("CONTROL_PLANE_URL", "")
        self.sandbox_token = os.environ.get("SANDBOX_AUTH_TOKEN", "")
        self.repo_owner = os.environ.get("REPO_OWNER", "")
        self.repo_name = os.environ.get("REPO_NAME", "")
        self.vcs_host = os.environ.get("VCS_HOST", "github.com")
        self.vcs_clone_username = os.environ.get("VCS_CLONE_USERNAME", "x-access-token")
        self.vcs_clone_token = os.environ.get("VCS_CLONE_TOKEN") or os.environ.get(
            "GITHUB_APP_TOKEN", ""
        )

        # Parse session config if provided
        session_config_json = os.environ.get("SESSION_CONFIG", "{}")
        self.session_config = json.loads(session_config_json)

        # Paths
        self.workspace_path = Path("/workspace")
        self.repo_path = self.workspace_path / self.repo_name

        # Logger
        session_id = self.session_config.get("session_id", "")
        self.log = get_logger(
            "supervisor",
            service="sandbox",
            sandbox_id=self.sandbox_id,
            session_id=session_id,
        )

    @property
    def base_branch(self) -> str:
        """The branch to clone/fetch — defaults to 'main'."""
        return self.session_config.get("branch", "main")

    def _build_repo_url(self, authenticated: bool = True) -> str:
        """Build the HTTPS URL for the repository, optionally with clone credentials."""
        if authenticated and self.vcs_clone_token:
            return f"https://{self.vcs_clone_username}:{self.vcs_clone_token}@{self.vcs_host}/{self.repo_owner}/{self.repo_name}.git"
        return f"https://{self.vcs_host}/{self.repo_owner}/{self.repo_name}.git"

    def _redact_git_stderr(self, stderr_text: str) -> str:
        """Redact credential-bearing URLs from git stderr."""
        redacted_stderr = stderr_text
        if self.vcs_clone_token:
            redacted_stderr = redacted_stderr.replace(
                self._build_repo_url(),
                self._build_repo_url(authenticated=False),
            )
            redacted_stderr = redacted_stderr.replace(self.vcs_clone_token, "***")

        return re.sub(r"(https?://)([^/\s@]+)@", r"\1***@", redacted_stderr)

    # ------------------------------------------------------------------
    # Git primitives
    # ------------------------------------------------------------------

    async def _clone_repo(self) -> bool:
        """Shallow-clone the repository."""
        self.log.info(
            "git.clone_start",
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
            authenticated=bool(self.vcs_clone_token),
        )

        result = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth",
            str(self.CLONE_DEPTH_COMMITS),
            "--branch",
            self.base_branch,
            self._build_repo_url(),
            str(self.repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await result.communicate()

        if result.returncode != 0:
            self.log.error(
                "git.clone_error",
                stderr=self._redact_git_stderr(stderr.decode()),
                exit_code=result.returncode,
            )
            return False

        self.log.info("git.clone_complete", repo_path=str(self.repo_path))
        return True

    async def _ensure_remote_auth(self) -> None:
        """Set the remote URL with auth credentials if a clone token is available."""
        if not self.vcs_clone_token:
            return
        proc = await asyncio.create_subprocess_exec(
            "git",
            "remote",
            "set-url",
            "origin",
            self._build_repo_url(),
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            self.log.warn(
                "git.set_url_failed",
                exit_code=proc.returncode,
                stderr=self._redact_git_stderr(stderr.decode()),
            )

    async def _fetch_branch(self, branch: str) -> bool:
        """Fetch a branch with an explicit refspec.

        Uses an explicit refspec so that ``refs/remotes/origin/<branch>`` is
        created even in shallow or single-branch clones.
        """
        result = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            "origin",
            f"{branch}:refs/remotes/origin/{branch}",
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await result.communicate()
        if result.returncode != 0:
            self.log.error(
                "git.fetch_error",
                stderr=self._redact_git_stderr(stderr.decode()),
                exit_code=result.returncode,
            )
            return False
        return True

    async def _checkout_branch(self, branch: str) -> bool:
        """Create/reset a local branch to match the remote tip."""
        result = await asyncio.create_subprocess_exec(
            "git",
            "checkout",
            "-B",
            branch,
            f"origin/{branch}",
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await result.communicate()
        if result.returncode != 0:
            self.log.warn(
                "git.checkout_error",
                stderr=self._redact_git_stderr(stderr.decode()),
                exit_code=result.returncode,
                target_branch=branch,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Git sync methods (compose the primitives above)
    # ------------------------------------------------------------------

    async def _update_existing_repo(self) -> bool:
        """Fetch the target branch and check it out in an existing repo.

        Used by both snapshot-restore and repo-image boot paths where the
        repository already exists on disk.
        """
        if not self.repo_path.exists():
            self.log.info("git.update_skip", reason="no_repo_path")
            return False

        try:
            await self._ensure_remote_auth()
            branch = self.base_branch
            if not await self._fetch_branch(branch):
                return False
            return await self._checkout_branch(branch)
        except Exception as e:
            self.log.error("git.update_error", exc=e)
            return False

    async def perform_git_sync(self) -> bool:
        """Clone repository if needed, then sync to the target branch.

        Returns:
            True if sync completed successfully, False otherwise.
        """
        self.log.debug(
            "git.sync_start",
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
            repo_path=str(self.repo_path),
            has_clone_token=bool(self.vcs_clone_token),
        )

        if not self.repo_path.exists():
            if not self.repo_owner or not self.repo_name:
                self.log.info("git.skip_clone", reason="no_repo_configured")
                return True
            if not await self._clone_repo():
                return False

        return await self._update_existing_repo()

    def _setup_claude_auth(self) -> None:
        """Validate Claude OAuth credentials from volume mount and write settings.

        The claude-auth-vol Modal Volume is mounted at /root/.claude,
        providing .credentials.json with OAuth tokens. This method validates
        the credentials exist and writes a Claude Code settings.json for
        sandbox use.
        """
        claude_dir = Path("/root/.claude")
        creds_file = claude_dir / ".credentials.json"

        if not creds_file.exists():
            self.log.warn("claude_auth.missing_credentials", path=str(creds_file))
            return

        self.log.info("claude_auth.credentials_found", path=str(creds_file))

        # Write Claude Code settings for sandbox operation
        settings = {
            "permissions": {
                "allow": ["Bash", "Read", "Edit", "Write", "Glob", "Grep",
                          "WebFetch", "WebSearch"],
                "deny": [],
            },
        }

        settings_file = claude_dir / "settings.json"
        try:
            settings_file.write_text(json.dumps(settings, indent=2))
            self.log.info("claude_auth.settings_written")
        except Exception as e:
            self.log.warn("claude_auth.settings_write_error", exc=e)

    async def start_code_server(self) -> None:
        """Start code-server for browser-based VS Code editing."""
        password = os.environ.get("CODE_SERVER_PASSWORD")
        if not password:
            self.log.info("code_server.skip", reason="no_password")
            return

        # Use repo path if cloned, otherwise /workspace
        workdir = self.workspace_path
        if self.repo_path.exists() and (self.repo_path / ".git").exists():
            workdir = self.repo_path

        self.code_server_process = await asyncio.create_subprocess_exec(
            "code-server",
            "--bind-addr",
            f"0.0.0.0:{CODE_SERVER_PORT}",
            "--auth",
            "password",
            "--disable-telemetry",
            str(workdir),
            cwd=workdir,
            env={**os.environ, "PASSWORD": password},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        asyncio.create_task(self._forward_code_server_logs())
        self.log.info("code_server.started", port=CODE_SERVER_PORT)

    async def _forward_code_server_logs(self) -> None:
        """Forward code-server stdout to supervisor stdout."""
        if not self.code_server_process or not self.code_server_process.stdout:
            return

        try:
            async for line in self.code_server_process.stdout:
                self.log.info("code_server.stdout", line=line.decode().rstrip())
        except Exception as e:
            self.log.warn("code_server.log_forward_error", exc=e)

    async def start_bridge(self) -> None:
        """Start the agent bridge process."""
        self.log.info("bridge.start")

        if not self.control_plane_url:
            self.log.info("bridge.skip", reason="no_control_plane_url")
            return

        # Get session_id from config (required for WebSocket connection)
        session_id = self.session_config.get("session_id", "")
        if not session_id:
            self.log.info("bridge.skip", reason="no_session_id")
            return

        # Run bridge as a module (works with relative imports)
        self.bridge_process = await asyncio.create_subprocess_exec(
            "python",
            "-m",
            "sandbox_runtime.bridge",
            "--sandbox-id",
            self.sandbox_id,
            "--session-id",
            session_id,
            "--control-plane",
            self.control_plane_url,
            "--token",
            self.sandbox_token,
            env=os.environ,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Start log forwarder for bridge
        asyncio.create_task(self._forward_bridge_logs())
        self.log.info("bridge.started")

        # Check if bridge exited immediately during startup
        await asyncio.sleep(0.5)
        if self.bridge_process.returncode is not None:
            exit_code = self.bridge_process.returncode
            # Bridge exited immediately - read any error output
            stdout, _ = await self.bridge_process.communicate()
            if exit_code == 0:
                self.log.warn("bridge.early_exit", exit_code=exit_code)
            else:
                self.log.error(
                    "bridge.startup_crash",
                    exit_code=exit_code,
                    output=stdout.decode() if stdout else "",
                )

    async def _forward_bridge_logs(self) -> None:
        """Forward bridge stdout to supervisor stdout."""
        if not self.bridge_process or not self.bridge_process.stdout:
            return

        try:
            async for line in self.bridge_process.stdout:
                # Bridge already prefixes its output with [bridge], don't double it
                print(line.decode().rstrip())
        except Exception as e:
            print(f"[supervisor] Bridge log forwarding error: {e}")

    async def monitor_processes(self) -> None:
        """Monitor child processes and restart on crash."""
        bridge_restart_count = 0
        code_server_restart_count = 0

        while not self.shutdown_event.is_set():
            # Check bridge process
            if self.bridge_process and self.bridge_process.returncode is not None:
                exit_code = self.bridge_process.returncode

                if exit_code == 0:
                    # Graceful exit: shutdown command, session terminated, or fatal
                    # connection error. Propagate shutdown rather than restarting.
                    self.log.info(
                        "bridge.graceful_exit",
                        exit_code=exit_code,
                    )
                    self.shutdown_event.set()
                    break
                else:
                    # Crash: restart with backoff and retry limit
                    bridge_restart_count += 1
                    self.log.error(
                        "bridge.crash",
                        exit_code=exit_code,
                        restart_count=bridge_restart_count,
                    )

                    if bridge_restart_count > self.MAX_RESTARTS:
                        self.log.error(
                            "bridge.max_restarts",
                            restart_count=bridge_restart_count,
                        )
                        await self._report_fatal_error(
                            f"Bridge crashed {bridge_restart_count} times, giving up"
                        )
                        self.shutdown_event.set()
                        break

                    delay = min(self.BACKOFF_BASE**bridge_restart_count, self.BACKOFF_MAX)
                    self.log.info(
                        "bridge.restart",
                        delay_s=round(delay, 1),
                        restart_count=bridge_restart_count,
                    )
                    await asyncio.sleep(delay)
                    await self.start_bridge()

            # Check code-server process (non-fatal, best-effort restart)
            if self.code_server_process and self.code_server_process.returncode is not None:
                code_server_restart_count += 1
                self.log.warn(
                    "code_server.crash",
                    exit_code=self.code_server_process.returncode,
                    restart_count=code_server_restart_count,
                )

                if code_server_restart_count <= self.MAX_RESTARTS:
                    delay = min(self.BACKOFF_BASE**code_server_restart_count, self.BACKOFF_MAX)
                    await asyncio.sleep(delay)
                    try:
                        await self.start_code_server()
                    except Exception as e:
                        self.log.warn("code_server.restart_failed", exc=e)
                        self.code_server_process = None
                else:
                    self.log.warn(
                        "code_server.max_restarts", restart_count=code_server_restart_count
                    )
                    self.code_server_process = None

            await asyncio.sleep(1.0)

    async def _report_fatal_error(self, message: str) -> None:
        """Report a fatal error to the control plane."""
        self.log.error("supervisor.fatal", message=message)

        if not self.control_plane_url:
            return

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self.control_plane_url}/sandbox/{self.sandbox_id}/error",
                    json={"error": message, "fatal": True},
                    headers={"Authorization": f"Bearer {self.sandbox_token}"},
                    timeout=5.0,
                )
        except Exception as e:
            self.log.error("supervisor.report_error_failed", exc=e)

    def _hook_env(self) -> dict[str, str]:
        """Build environment for startup hooks."""
        env = os.environ.copy()
        env["OPENINSPECT_BOOT_MODE"] = self.boot_mode
        return env

    async def _run_hook(
        self,
        *,
        hook_name: str,
        relative_script_path: str,
        timeout_env_var: str,
        default_timeout_seconds: int,
    ) -> bool:
        """
        Run a repo hook script if present.

        Returns:
            True if script succeeded or was not present, False on failure/timeout.
        """
        script_path = self.repo_path / relative_script_path
        start_time = time.time()

        if not script_path.exists():
            self.log.debug(
                f"{hook_name}.skip",
                reason="no_script",
                path=str(script_path),
                boot_mode=self.boot_mode,
            )
            return True

        try:
            timeout_seconds = int(os.environ.get(timeout_env_var, str(default_timeout_seconds)))
        except ValueError:
            timeout_seconds = default_timeout_seconds

        self.log.info(
            f"{hook_name}.start",
            script=str(script_path),
            timeout_seconds=timeout_seconds,
            boot_mode=self.boot_mode,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                "bash",
                str(script_path),
                cwd=self.repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._hook_env(),
            )

            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except TimeoutError:
                process.kill()
                stdout = await process.stdout.read() if process.stdout else b""
                await process.wait()
                output_tail = "\n".join(stdout.decode(errors="replace").splitlines()[-50:])
                duration_ms = int((time.time() - start_time) * 1000)
                self.log.error(
                    f"{hook_name}.timeout",
                    timeout_seconds=timeout_seconds,
                    output_tail=output_tail,
                    script=str(script_path),
                    duration_ms=duration_ms,
                    boot_mode=self.boot_mode,
                )
                return False

            output_tail = "\n".join(
                (stdout.decode(errors="replace") if stdout else "").splitlines()[-50:]
            )
            duration_ms = int((time.time() - start_time) * 1000)

            if process.returncode == 0:
                # Avoid logging hook stdout at info level to reduce secret exposure risk.
                self.log.info(
                    f"{hook_name}.complete",
                    exit_code=0,
                    script=str(script_path),
                    duration_ms=duration_ms,
                    boot_mode=self.boot_mode,
                )
                return True

            self.log.error(
                f"{hook_name}.failed",
                exit_code=process.returncode,
                output_tail=output_tail,
                script=str(script_path),
                duration_ms=duration_ms,
                boot_mode=self.boot_mode,
            )
            return False

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self.log.error(
                f"{hook_name}.error",
                exc=e,
                script=str(script_path),
                duration_ms=duration_ms,
                boot_mode=self.boot_mode,
            )
            return False

    async def run_setup_script(self) -> bool:
        """
        Run .openinspect/setup.sh if it exists in the cloned repo.

        Fresh-session failures are non-fatal. Build mode callers may treat
        failures as fatal.

        Returns:
            True if script succeeded or was not present, False on failure/timeout.
        """
        return await self._run_hook(
            hook_name="setup",
            relative_script_path=self.SETUP_SCRIPT_PATH,
            timeout_env_var="SETUP_TIMEOUT_SECONDS",
            default_timeout_seconds=self.DEFAULT_SETUP_TIMEOUT_SECONDS,
        )

    async def run_start_script(self) -> bool:
        """
        Run .openinspect/start.sh if it exists in the repository.

        Returns:
            True if script succeeded or was not present, False on failure/timeout.
        """
        return await self._run_hook(
            hook_name="start",
            relative_script_path=self.START_SCRIPT_PATH,
            timeout_env_var="START_TIMEOUT_SECONDS",
            default_timeout_seconds=self.DEFAULT_START_TIMEOUT_SECONDS,
        )

    async def run(self) -> None:
        """Main supervisor loop."""
        startup_start = time.time()

        self.log.info(
            "supervisor.start",
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
        )

        # Detect operating mode
        image_build_mode = os.environ.get("IMAGE_BUILD_MODE") == "true"
        restored_from_snapshot = os.environ.get("RESTORED_FROM_SNAPSHOT") == "true"
        from_repo_image = os.environ.get("FROM_REPO_IMAGE") == "true"

        if image_build_mode:
            self.boot_mode = "build"
        elif restored_from_snapshot:
            self.boot_mode = "snapshot_restore"
        elif from_repo_image:
            self.boot_mode = "repo_image"
        else:
            self.boot_mode = "fresh"

        # Expose boot mode to repo hooks and child processes.
        os.environ["OPENINSPECT_BOOT_MODE"] = self.boot_mode

        if image_build_mode:
            self.log.info("supervisor.image_build_mode")
        elif restored_from_snapshot:
            self.log.info("supervisor.restored_from_snapshot")
        elif from_repo_image:
            repo_image_sha = os.environ.get("REPO_IMAGE_SHA", "unknown")
            self.log.info("supervisor.from_repo_image", build_sha=repo_image_sha)

        # Set up signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._handle_signal(s)))

        git_sync_success = False
        try:
            # Phase 1: Git sync
            if restored_from_snapshot:
                await self._update_existing_repo()  # best-effort
                git_sync_success = True
            elif from_repo_image:
                git_sync_success = await self._update_existing_repo()
            else:
                git_sync_success = await self.perform_git_sync()
            self.git_sync_complete.set()

            # Phase 2: Run setup script only for fresh or build boots.
            setup_success: bool | None = None
            if self.boot_mode in ("fresh", "build"):
                setup_success = await self.run_setup_script()
                if image_build_mode and not setup_success:
                    raise RuntimeError("setup hook failed in build mode")

            # Phase 3: Run runtime start hook for all non-build boots.
            start_success: bool | None = None
            if self.boot_mode != "build":
                start_success = await self.run_start_script()
                if not start_success:
                    raise RuntimeError("start hook failed")
            else:
                start_success = None

            # Image build mode: signal completion, then keep sandbox alive for
            # snapshot_filesystem(). The builder streams stdout, detects this
            # event, snapshots the running sandbox, then terminates us.
            if image_build_mode:
                duration_ms = int((time.time() - startup_start) * 1000)
                self.log.info("image_build.complete", duration_ms=duration_ms)
                await self.shutdown_event.wait()
                return

            # Phase 3.5: Start code-server (non-blocking, no health check needed)
            await self.start_code_server()

            # Phase 4: Set up Claude Code auth (OAuth credentials from volume)
            self._setup_claude_auth()

            # Phase 5: Start bridge (no agent server to wait for — SDK launches
            # Claude Code on demand when prompts arrive)
            await self.start_bridge()

            # Emit sandbox.startup wide event
            duration_ms = int((time.time() - startup_start) * 1000)
            self.log.info(
                "sandbox.startup",
                repo_owner=self.repo_owner,
                repo_name=self.repo_name,
                boot_mode=self.boot_mode,
                restored_from_snapshot=restored_from_snapshot,
                from_repo_image=from_repo_image,
                git_sync_success=git_sync_success,
                setup_success=setup_success,
                start_success=start_success,
                duration_ms=duration_ms,
                outcome="success",
            )

            # Phase 6: Monitor processes
            await self.monitor_processes()

        except Exception as e:
            self.log.error("supervisor.error", exc=e)
            await self._report_fatal_error(str(e))

        finally:
            await self.shutdown()

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal."""
        self.log.info("supervisor.signal", signal_name=sig.name)
        self.shutdown_event.set()

    async def shutdown(self) -> None:
        """Graceful shutdown of all processes."""
        self.log.info("supervisor.shutdown_start")

        # Terminate bridge first
        if self.bridge_process and self.bridge_process.returncode is None:
            self.bridge_process.terminate()
            try:
                await asyncio.wait_for(self.bridge_process.wait(), timeout=5.0)
            except TimeoutError:
                self.bridge_process.kill()

        # Terminate code-server
        if self.code_server_process and self.code_server_process.returncode is None:
            self.code_server_process.terminate()
            try:
                await asyncio.wait_for(self.code_server_process.wait(), timeout=5.0)
            except TimeoutError:
                self.code_server_process.kill()

        self.log.info("supervisor.shutdown_complete")


async def main():
    """Entry point for the sandbox supervisor."""
    supervisor = SandboxSupervisor()
    await supervisor.run()


if __name__ == "__main__":
    asyncio.run(main())
