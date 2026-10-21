"""Background jobs: long commands that run while the agent keeps working.

bash blocks the turn until the command ends (at most ten minutes). A build,
a test suite, a data download or a training run can take longer, and the
agent has nothing to do but wait. job_start launches the command in the
background and returns at once; job_status, job_wait and job_stop follow it
up. Output goes to .xharness/jobs/<id>.log in the working directory, where
the read tool can page through it.

Jobs are confined by the same sandbox as bash, start under the same approval,
are capped in number and run time, and are killed when the conversation ends.
A job that finishes is announced with the person's next message, so the model
hears about it even if it never asked.
"""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - running commands is this module's purpose
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..context import Context, Plugin
from ..sandbox import Sandbox
from . import Tool, ToolContext, ToolResult, register_tools

MAX_RUNNING = 4
DEFAULT_MAX_SECONDS = 2 * 3600
TAIL_LINES = 40
MAX_TAIL_CHARS = 8000
MAX_WAIT_SECONDS = 600


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    log_path: str
    started: float
    process: subprocess.Popen | None = None
    state: str = "running"          # running | done | failed | stopped | timeout
    exit_code: int | None = None
    ended: float | None = None
    announced: bool = False
    finished: threading.Event = field(default_factory=threading.Event)

    def runtime(self) -> float:
        return round((self.ended or time.time()) - self.started, 1)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "command": self.command, "state": self.state, "exit_code": self.exit_code,
                "started": self.started, "runtime": self.runtime(), "log": self.log_path}


class JobManager:
    def __init__(self, sandbox: Sandbox | None = None, max_running: int = MAX_RUNNING,
                 max_seconds: float = DEFAULT_MAX_SECONDS, on_change: Callable[[Job], None] | None = None) -> None:
        self.sandbox = sandbox
        self.max_running = max_running
        self.max_seconds = max_seconds
        self.on_change = on_change
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def _notify(self, job: Job) -> None:
        if self.on_change:
            try:
                self.on_change(job)
            except Exception:  # noqa: BLE001 - a listener must not break the job  # nosec B110
                pass

    def running(self) -> list[Job]:
        return [job for job in self.jobs.values() if job.state == "running"]

    def start(self, command: str, cwd: str) -> Job:
        with self._lock:
            if len(self.running()) >= self.max_running:
                raise RuntimeError(f"already {self.max_running} jobs running; wait for one or stop one first")
            self._counter += 1
            job_id = f"j{self._counter}"
        directory = os.path.join(cwd, ".xharness", "jobs")
        os.makedirs(directory, exist_ok=True)
        ignore = os.path.join(cwd, ".xharness", ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as handle:
                handle.write("*\n")
        log_path = os.path.join(directory, f"{job_id}-{int(time.time())}.log")
        argv = ["bash", "-c", command]
        if self.sandbox is not None:
            argv = self.sandbox.wrap(argv, cwd)
        job = Job(job_id, command, cwd, log_path, time.time())
        log = open(log_path, "wb")  # closed by the watcher  # noqa: SIM115
        try:
            job.process = subprocess.Popen(  # nosec B603 - approved command, sandboxed when a backend exists
                argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            log.close()
            raise
        with self._lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._watch, args=(job, log), name=f"xharness-job-{job_id}", daemon=True).start()
        self._notify(job)
        return job

    def _watch(self, job: Job, log: Any) -> None:
        if job.process is None:
            return
        try:
            job.exit_code = job.process.wait(timeout=self.max_seconds)
            if job.state == "running":
                job.state = "done" if job.exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            self._kill(job)
            job.state = "timeout"
            job.exit_code = job.process.wait()
        finally:
            log.close()
            job.ended = time.time()
            job.finished.set()
            self._notify(job)

    def _kill(self, job: Job) -> None:
        if job.process is None or job.process.poll() is not None:
            return
        for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 0)):
            try:
                os.killpg(job.process.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                job.process.wait(timeout=grace or None)
                return
            except subprocess.TimeoutExpired:
                continue

    def stop(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None or job.state != "running":
            return job
        job.state = "stopped"
        self._kill(job)
        job.finished.wait(5)
        return job

    def wait(self, job_id: str, timeout: float) -> Job | None:
        job = self.jobs.get(job_id)
        if job is not None:
            job.finished.wait(timeout)
        return job

    def tail(self, job: Job, lines: int = TAIL_LINES) -> str:
        try:
            with open(job.log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 64_000))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])[-MAX_TAIL_CHARS:]

    def finished_unannounced(self) -> list[Job]:
        done = [job for job in self.jobs.values() if job.state != "running" and not job.announced]
        for job in done:
            job.announced = True
        return done

    def shutdown(self) -> None:
        for job in self.running():
            job.state = "stopped"
            self._kill(job)


STATE_WORDS = {"running": "running", "done": "finished (exit 0)", "failed": "failed", "stopped": "stopped",
               "timeout": "killed at the time limit"}


def describe(manager: JobManager, job: Job, with_tail: bool = True) -> str:
    head = (f"job {job.id}: {STATE_WORDS.get(job.state, job.state)}"
            + (f", exit {job.exit_code}" if job.exit_code is not None and job.state != "done" else "")
            + f", {job.runtime()}s\ncommand: {job.command}\nlog: {job.log_path}")
    if not with_tail:
        return head
    tail = manager.tail(job)
    return head + ("\n--- last lines ---\n" + tail if tail else "\n(no output yet)")


def jobs_plugin(settings: dict[str, Any] | None = None) -> Plugin:
    settings = settings or {}

    def apply(ctx: Context, _config: Any) -> None:
        manager = JobManager(
            sandbox=ctx.optional("sandbox"),
            max_running=int(settings.get("max_running", MAX_RUNNING)),
            max_seconds=float(settings.get("max_seconds", DEFAULT_MAX_SECONDS)),
            on_change=lambda job: ctx.emit("jobs/changed", job.public()),
        )
        ctx.provide("jobs", manager)
        ctx.on_dispose(manager.shutdown)

        def start(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            command = str(args.get("command") or "").strip()
            if not command:
                return ToolResult("command is required", is_error=True)
            if not tool_ctx.approve(f"background job: {command}"):
                return ToolResult("denied by approval policy", is_error=True)
            try:
                job = manager.start(command, tool_ctx.cwd)
            except (RuntimeError, OSError) as error:
                return ToolResult(f"cannot start job: {error}", is_error=True)
            return ToolResult(f"started job {job.id} in the background; output goes to {job.log_path}. "
                              "Keep working, then use job_status or job_wait to check on it.")

        def status(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            job_id = str(args.get("id") or "").strip()
            if job_id:
                job = manager.jobs.get(job_id)
                return ToolResult(describe(manager, job)) if job else ToolResult(f"no job {job_id}", is_error=True)
            if not manager.jobs:
                return ToolResult("no background jobs")
            return ToolResult("\n\n".join(describe(manager, job, with_tail=False) for job in manager.jobs.values()))

        def wait(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            job_id = str(args.get("id") or "").strip()
            timeout = min(float(args.get("timeout_seconds") or 60), MAX_WAIT_SECONDS)
            job = manager.wait(job_id, timeout)
            if job is None:
                return ToolResult(f"no job {job_id}", is_error=True)
            note = "" if job.state != "running" else f"\n(still running after waiting {timeout:g}s)"
            return ToolResult(describe(manager, job) + note)

        def stop(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            job_id = str(args.get("id") or "").strip()
            job = manager.stop(job_id)
            if job is None:
                return ToolResult(f"no job {job_id}", is_error=True)
            return ToolResult(describe(manager, job))

        id_param = {"id": {"type": "string", "description": "Job id, e.g. j1"}}
        register_tools(ctx, [
            Tool("job_start", "Run a long shell command in the background (builds, test suites, downloads). "
                 "Returns at once with a job id; the command keeps running while you work. Use bash for "
                 "anything quick.",
                 {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                 start, mutating=True),
            Tool("job_status", "State and last lines of output of one background job, or a list of all of them.",
                 {"type": "object", "properties": id_param}, status),
            Tool("job_wait", "Wait for a background job to finish (up to timeout_seconds, default 60, max 600).",
                 {"type": "object", "properties": {**id_param, "timeout_seconds": {"type": "number"}}, "required": ["id"]},
                 wait),
            Tool("job_stop", "Stop a background job.", {"type": "object", "properties": id_param, "required": ["id"]}, stop),
        ])

    return Plugin("jobs", apply)
