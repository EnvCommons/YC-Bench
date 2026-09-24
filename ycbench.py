import asyncio
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import List

from pydantic import BaseModel

from openreward.environments import Environment, JSONObject, ToolOutput, TextBlock, tool

# Pre-warm yc-bench's import graph (pydantic, sqlalchemy, typer, all subcommand
# modules) once at module load. Per-session setup then only pays for the
# actual sim work, not for cold Python + yc-bench imports every time.
from yc_bench.cli import app as _yc_app
from yc_bench.cli import _engine_cache as _yc_engine_cache
from yc_bench.config import load_config as _yc_load_config

MAX_COMMANDS = 5000
AUTO_RESUME_THRESHOLD = 30

# Total loss of starting capital. Funds can go arbitrarily negative, so clamp.
POTENTIAL_FLOOR = -1.0

_yc_invoke_lock = threading.Lock()


class _ThreadRoutedStream:
    """Per-thread stdout capture; a global redirect leaks other threads' logs into tool output."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def capture(self, buf):
        self._local.buf = buf

    def release(self):
        self._local.buf = None

    def _target(self):
        buf = getattr(self._local, "buf", None)
        return self._real if buf is None else buf

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self._target(), name)


def _routed(name: str) -> _ThreadRoutedStream:
    """Install the router on sys.<name>, re-wrapping if something replaced it since."""
    stream = getattr(sys, name)
    if not isinstance(stream, _ThreadRoutedStream):
        stream = _ThreadRoutedStream(stream)
        setattr(sys, name, stream)
    return stream


def _invoke_yc(argv: list[str], db_url: str, experiment: str) -> dict:
    """Dispatch a yc-bench command via the typer app in-process.

    yc-bench reads DATABASE_URL and YC_BENCH_EXPERIMENT from os.environ at
    call time, so we serialize all invocations on a pod-wide lock while we
    swap those env vars.
    """
    if argv and argv[0] == "yc-bench":
        argv = argv[1:]
    buf = io.StringIO()
    err_buf = io.StringIO()
    exit_code = 0
    with _yc_invoke_lock:
        prev_db = os.environ.get("DATABASE_URL")
        prev_exp = os.environ.get("YC_BENCH_EXPERIMENT")
        os.environ["DATABASE_URL"] = db_url
        os.environ["YC_BENCH_EXPERIMENT"] = experiment
        out, err = _routed("stdout"), _routed("stderr")
        out.capture(buf)
        err.capture(err_buf)
        try:
            _yc_app(argv, standalone_mode=False)
        except SystemExit as e:
            try:
                exit_code = int(e.code) if e.code is not None else 0
            except (TypeError, ValueError):
                exit_code = 1
        except Exception as e:
            exit_code = 1
            err_buf.write(f"{type(e).__name__}: {e}")
        finally:
            out.release()
            err.release()
            if prev_db is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev_db
            if prev_exp is None:
                os.environ.pop("YC_BENCH_EXPERIMENT", None)
            else:
                os.environ["YC_BENCH_EXPERIMENT"] = prev_exp
    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "stdout": buf.getvalue(),
        "stderr": err_buf.getvalue(),
    }


def _expected_delta(resume_outputs: list[str]) -> int | None:
    """Sum the `balance_delta` the engine reports across this step's resumes.

    None means unparseable, so skip the cross-check — not that no money moved.
    """
    total = 0
    for stdout in resume_outputs:
        try:
            total += int(json.loads(stdout.strip())["balance_delta"])
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return None
    return total


def _ledger_breakdown(rows: list) -> dict[str, int]:
    """Aggregate ledger rows by economic meaning.

    yc-bench records deadline-miss penalties as negative `task_reward` rows, so
    direction comes from the sign rather than the category.
    """
    totals: dict[str, int] = {}
    for row in rows:
        try:
            amount = int(row["amount_cents"])
            category = str(row["category"])
        except (KeyError, TypeError, ValueError):
            continue
        if category == "task_reward" and amount < 0:
            category = "task_fail_penalty"
        totals[category] = totals.get(category, 0) + amount
    return totals


SYSTEM_PROMPT = """\
You are the CEO of a startup in a business simulation.

Your objective: maximize the company's funds relative to your starting capital, avoiding bankruptcy.
You are scored on profit as a fraction of the capital you began with. Prestige and client trust are
instrumental — they gate and scale task payouts — but they are not scored directly.

All actions use `yc-bench` CLI commands via `run_command`. All return JSON.

## Core Workflow (repeat every turn)

**You must always have active tasks running. Every turn, follow this loop:**

1. `yc-bench market browse` — pick a task
2. `yc-bench task accept --task-id Task-42` — accept it
3. `yc-bench task assign --task-id Task-42 --employees Emp_1,Emp_4,Emp_7` — assign employees (check `employee list` for skill rates)
4. `yc-bench task dispatch --task-id Task-42` — start work
5. `yc-bench sim resume` — advance to next event (requires active tasks)

Run multiple tasks concurrently when possible. Accept → assign → dispatch a second task before calling sim resume.

**Use `yc-bench scratchpad write`** to save strategy notes — your conversation history is truncated after 20 turns, but scratchpad persists in the system prompt. Write reusable rules, not one-off observations.

## Commands

### Observe
- `yc-bench company status` — funds, prestige, payroll
- `yc-bench employee list` — employees with skill rates per domain
- `yc-bench market browse [--domain X] [--reward-min-cents N] [--limit N]` — available tasks
- `yc-bench task list [--status X]` — your tasks
- `yc-bench task inspect --task-id Task-42` — task details
- `yc-bench client list` — clients with trust levels
- `yc-bench client history` — per-client success/failure rates
- `yc-bench finance ledger` — financial history

### Act
- `yc-bench task accept --task-id Task-42` — accept from market
- `yc-bench task assign --task-id Task-42 --employees Emp_1,Emp_4,Emp_7` — assign employees (comma-separated)
- `yc-bench task dispatch --task-id Task-42` — start work (must assign first)
- `yc-bench task cancel --task-id Task-42 --reason "text"` — cancel (prestige penalty)
- `yc-bench sim resume` — advance time
- `yc-bench scratchpad write --content "text"` — save notes
- `yc-bench scratchpad append --content "text"` — append notes

## Key Mechanics

- **Salary bumps**: completed tasks raise salary for every assigned employee. More employees assigned = higher payroll growth.
- **Throughput split**: employees on multiple active tasks split their rate (rate/N). Two tasks run at 50% each.
- **Deadlines**: success before deadline = reward + prestige. Failure = prestige penalty, no reward.
- **Trust**: completing tasks for a client builds trust → less work per task, access to gated tasks. Working for one client erodes trust with others.
- **Not all clients are reliable.** Check `client history` for failure patterns.
- **Payroll**: deducted monthly. Funds < 0 = bankruptcy.
- Prestige grows per domain. Higher prestige unlocks better-paying tasks.
"""


class TaskSpec(BaseModel):
    id: str
    preset: str
    seed: int


class RunCommandInput(BaseModel, extra="forbid"):
    command: str


class YCBench(Environment):
    def __init__(self, task_spec: JSONObject, secrets: dict[str, str] = {}) -> None:
        super().__init__(task_spec)
        self.validated = TaskSpec.model_validate(task_spec)
        self.preset = self.validated.preset
        self.seed = self.validated.seed

        # Session-unique database
        self.db_dir = tempfile.mkdtemp(prefix="ycbench_")
        self.db_path = Path(self.db_dir) / "yc_bench.db"
        self.db_url = f"sqlite:///{self.db_path}"

        # State tracking
        self.initial_funds_cents: int = 0
        self.command_count: int = 0
        self.commands_since_resume: int = 0
        self.finished: bool = False
        self.terminal_reason: str | None = None

        # Step rewards are potential deltas, so their sum is the current potential.
        self.ledger_cursor: int = 0
        self.ledger_total_cents: int = 0
        self.potential: float = 0.0

        # Initialize simulation
        self._init_simulation()

    def _execute_command(self, command: str) -> dict:
        """Run a yc-bench command in-process via the typer app."""
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return {"ok": False, "exit_code": 2, "stdout": "", "stderr": str(e)}
        return _invoke_yc(argv, self.db_url, self.preset)

    def _load_config_values(self) -> dict:
        """Read the values the simulation needs from the yc-bench preset.

        Raises on an unloadable preset: guessing a horizon would silently build
        a simulation of the wrong length.
        """
        cfg = _yc_load_config(self.preset)
        return {
            "horizon_years": cfg.sim.horizon_years,
            "initial_funds_cents": cfg.world.initial_funds_cents,
        }

    def _read_funds_cents(self) -> int | None:
        """Read the authoritative balance from `company status`."""
        status = self._execute_command("yc-bench company status")
        if not status["ok"]:
            return None
        try:
            return int(json.loads(status["stdout"].strip())["funds_cents"])
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return None

    def _init_simulation(self) -> None:
        """Initialize the yc-bench simulation via the in-process CLI."""
        config_vals = self._load_config_values()
        horizon_years = config_vals["horizon_years"]

        result = self._execute_command(
            f"yc-bench sim init "
            f"--seed {self.seed} "
            f"--start-date 01/01/2025 "
            f"--horizon-years {horizon_years} "
            f"--company-name BenchCo"
        )
        if not result["ok"]:
            raise RuntimeError(
                f"Failed to initialize yc-bench simulation: {result['stderr'] or result['stdout']}"
            )

        # Every reward is normalized by this, so never let it be zero.
        funds = self._read_funds_cents()
        if funds is None:
            funds = config_vals["initial_funds_cents"]
        if funds <= 0:
            raise RuntimeError(
                "Could not determine initial funds for preset "
                f"{self.preset!r}; refusing to start with an unnormalizable reward."
            )
        self.initial_funds_cents = funds

    def _check_terminal(self, stdout: str) -> ToolOutput | None:
        """Parse sim resume JSON for terminal conditions."""
        try:
            payload = json.loads(stdout.strip())
        except (json.JSONDecodeError, ValueError):
            return None

        terminal_reason = payload.get("terminal_reason")
        if terminal_reason in ("bankruptcy", "horizon_end"):
            # A successful resume resets the auto-resume counter, so this
            # payload's delta is the whole of what moved in this step.
            return self._force_terminal(
                terminal_reason,
                extra_text=stdout,
                expected_delta_cents=_expected_delta([stdout]),
            )
        return None

    def _potential_for(self, funds_cents: int) -> float:
        """Normalized profit against starting capital, floored at total loss."""
        profit = funds_cents - self.initial_funds_cents
        return max(POTENTIAL_FLOOR, profit / self.initial_funds_cents)

    def _flush_ledger(self, expected_delta_cents: int | None = None) -> tuple[float | None, dict]:
        """Consume new ledger rows and return the telescoping step reward.

        The ledger records every change to funds after seeding, so
        `funds == initial_funds + sum(amount_cents)` holds and no balance read
        is needed. Returns (None, ...) if unreadable, leaving the cursor
        untouched so the next flush catches up.
        """
        result = self._execute_command("yc-bench finance ledger")
        try:
            payload = json.loads(result.get("stdout", "").strip())
            entries = payload["entries"]
            total_cents = int(payload["total_amount_cents"])
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return None, {"ledger_read_failed": True}

        # One payroll writes a row per employee at an identical `occurred_at`
        # and the ledger has no secondary sort key, so count the rows and diff
        # the totals rather than relying on their order.
        new_rows = entries[self.ledger_cursor:]
        delta_cents = total_cents - self.ledger_total_cents
        self.ledger_cursor = len(entries)
        self.ledger_total_cents = total_cents

        step_reward = self._advance_potential(
            self._potential_for(self.initial_funds_cents + total_cents)
        )

        meta = {
            "step_reward": step_reward,
            "episode_return": self.potential,
            "funds_cents": self.initial_funds_cents + total_cents,
            "ledger_delta_cents": delta_cents,
            "ledger_rows": len(new_rows),
            "ledger_breakdown": _ledger_breakdown(new_rows),
        }
        if expected_delta_cents is not None and expected_delta_cents != delta_cents:
            meta["ledger_balance_delta_mismatch"] = {
                "engine_balance_delta": expected_delta_cents,
                "ledger_delta": delta_cents,
            }
        return step_reward, meta

    def _advance_potential(self, potential: float) -> float:
        """Move to a new potential and return the delta to emit as reward."""
        step_reward = potential - self.potential
        self.potential = potential
        return step_reward

    def _force_terminal(
        self,
        reason: str,
        extra_text: str = "",
        expected_delta_cents: int | None = None,
    ) -> ToolOutput:
        """End the simulation, emitting the residual telescoping delta.

        Never the cumulative return, which would double-count against the sum.
        """
        self.finished = True
        self.terminal_reason = reason
        step_reward, ledger_meta = self._flush_ledger(expected_delta_cents)

        if step_reward is None:
            # The residual is the one delta worth a second attempt.
            funds = self._read_funds_cents()
            if funds is not None:
                step_reward = self._advance_potential(self._potential_for(funds))
                ledger_meta = {
                    **ledger_meta,
                    "step_reward": step_reward,
                    "episode_return": self.potential,
                    "funds_cents": funds,
                    "reward_source": "company_status",
                }

        text = extra_text or ""
        text += (
            f"\n\n=== SIMULATION ENDED ===\n"
            f"Reason: {reason}\n"
            f"Episode return: {self.potential:.4f}"
        )

        return ToolOutput(
            blocks=[TextBlock(text=text)],
            metadata={
                **ledger_meta,
                "terminal_reason": reason,
                "command_count": self.command_count,
            },
            reward=step_reward,
            finished=True,
        )

    async def get_prompt(self) -> List[TextBlock]:
        # Off the event loop: the call may wait on the pod-wide lock.
        status = await asyncio.to_thread(self._execute_command, "yc-bench company status")
        initial_state = status.get("stdout", "") if status["ok"] else "Could not load initial state."

        prompt_text = (
            SYSTEM_PROMPT
            + "\n\n## Initial Simulation State\n\n"
            + initial_state
            + "\n\nYou have one tool available: `run_command`. "
            "Pass any `yc-bench <subcommand>` CLI command as the `command` parameter. "
            "All commands return JSON. "
            "Start by browsing the market and accepting tasks."
        )
        return [TextBlock(text=prompt_text)]

    @tool
    def run_command(self, params: RunCommandInput) -> ToolOutput:
        """Execute a yc-bench CLI command. Pass the full command string including 'yc-bench' prefix."""
        if self.finished:
            return ToolOutput(
                blocks=[TextBlock(text=f"Simulation already ended: {self.terminal_reason}")],
                metadata={"terminal_reason": self.terminal_reason},
                finished=True,
            )

        # Max command limit
        if self.command_count >= MAX_COMMANDS:
            return self._force_terminal("max_commands")

        command = params.command.strip()

        # Validate. shlex says exactly what is wrong ("No closing quotation"),
        # which the agent needs to repair a long quoted argument.
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return ToolOutput(
                blocks=[TextBlock(text=json.dumps({"error": f"Invalid command syntax: {e}"}))],
                metadata={"error": "invalid_syntax", "detail": str(e)},
                finished=False,
            )

        if not argv or argv[0] != "yc-bench":
            return ToolOutput(
                blocks=[TextBlock(text=json.dumps({"error": "Only yc-bench commands are allowed. Start your command with 'yc-bench'."}))],
                metadata={"error": "not_yc_bench"},
                finished=False,
            )

        # Execute
        result = self._execute_command(command)
        self.command_count += 1

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", 1)

        # Only a resume can move money, so only a resume can produce a reward.
        is_resume = len(argv) >= 3 and argv[1] == "sim" and argv[2] == "resume"
        resume_outputs: list[str] = []

        if is_resume and exit_code == 0:
            self.commands_since_resume = 0
            resume_outputs.append(stdout)
            terminal = self._check_terminal(stdout)
            if terminal:
                return terminal
        else:
            self.commands_since_resume += 1

        # Auto-resume check
        auto_resume_text = ""
        if self.commands_since_resume >= AUTO_RESUME_THRESHOLD:
            auto_result = self._execute_command("yc-bench sim resume")
            self.command_count += 1
            self.commands_since_resume = 0
            auto_stdout = auto_result.get("stdout", "")
            if auto_result.get("exit_code", 1) == 0:
                resume_outputs.append(auto_stdout)
                terminal = self._check_terminal(auto_stdout)
                if terminal:
                    return terminal
                auto_resume_text = (
                    f"\n\n[AUTO-RESUME triggered after {AUTO_RESUME_THRESHOLD} "
                    f"commands without sim resume]\n{auto_stdout}"
                )

        step_reward: float | None = None
        ledger_meta: dict = {}
        if resume_outputs:
            step_reward, ledger_meta = self._flush_ledger(_expected_delta(resume_outputs))

        # Build output
        output_text = stdout if exit_code == 0 else (stderr or stdout or "Command failed")
        output_text += auto_resume_text
        if step_reward is not None:
            output_text += (
                f"\n\n[cash flow: {ledger_meta.get('ledger_delta_cents', 0):+d}c | "
                f"step reward: {step_reward:+.4f} | return: {self.potential:+.4f}]"
            )

        return ToolOutput(
            blocks=[TextBlock(text=output_text)],
            metadata={
                **ledger_meta,
                "exit_code": exit_code,
                "command": command,
                "command_count": self.command_count,
            },
            reward=step_reward,
            finished=False,
        )

    async def teardown(self) -> None:
        # Off the event loop: waiting on the pod-wide lock would stall every session.
        await asyncio.to_thread(self._teardown_sync)

    def _teardown_sync(self) -> None:
        with _yc_invoke_lock:
            engine = _yc_engine_cache.pop(self.db_url, None)
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        if self.db_dir and Path(self.db_dir).exists():
            shutil.rmtree(self.db_dir, ignore_errors=True)

    @classmethod
    def list_tasks(cls, split: str) -> list[JSONObject]:
        # Upstream yc-bench collapsed to a single `default` preset in PR #21
        # (2026-04-01); the old `easy` / 1-year-horizon preset no longer ships.
        # Both splits use `default` now, with disjoint seeds so train and test
        # remain independent.
        if split == "train":
            seeds = [1, 2, 3]
        elif split == "test":
            seeds = [4, 5, 6]
        else:
            raise ValueError(f"Unknown split: {split}")

        return [
            {"id": f"default_{seed}", "preset": "default", "seed": seed}
            for seed in seeds
        ]

    @classmethod
    def list_splits(cls) -> list[str]:
        return ["train", "test"]
