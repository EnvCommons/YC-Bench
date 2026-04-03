import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

from pydantic import BaseModel

from openreward.environments import Environment, JSONObject, ToolOutput, TextBlock, tool

MAX_COMMANDS = 5000
AUTO_RESUME_THRESHOLD = 30

# Resolve the yc-bench binary path once at import time
_YC_BENCH_BIN = os.environ.get("YC_BENCH_BIN", "/tmp/ycbench-venv/bin/yc-bench")

SYSTEM_PROMPT = """\
You are the CEO of a startup in a business simulation. Maximize funds and prestige while avoiding bankruptcy.

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

        # Initialize simulation
        self._init_simulation()

    def _get_env(self) -> dict[str, str]:
        """Build subprocess environment with session-specific DB and config."""
        env = os.environ.copy()
        env["DATABASE_URL"] = self.db_url
        env["YC_BENCH_EXPERIMENT"] = self.preset
        return env

    def _execute_command(self, command: str) -> dict:
        """Run a yc-bench command via subprocess."""
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return {"ok": False, "exit_code": 2, "stdout": "", "stderr": str(e)}

        if argv and argv[0] == "yc-bench":
            argv[0] = _YC_BENCH_BIN

        try:
            proc = subprocess.run(
                argv,
                shell=False,
                text=True,
                capture_output=True,
                timeout=60.0,
                env=self._get_env(),
            )
            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "exit_code": 124, "stdout": "", "stderr": "command timed out"}
        except Exception as exc:
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": str(exc)}

    def _load_config_values(self) -> dict:
        """Read config values from the yc-bench preset using the Python 3.12 venv."""
        python_bin = str(Path(_YC_BENCH_BIN).parent / "python")
        script = (
            "from yc_bench.config import load_config; import json, os; "
            f"c = load_config('{self.preset}'); "
            "print(json.dumps({'horizon_years': c.sim.horizon_years}))"
        )
        try:
            proc = subprocess.run(
                [python_bin, "-c", script],
                capture_output=True, text=True, timeout=15.0,
                env=self._get_env(),
            )
            if proc.returncode == 0:
                return json.loads(proc.stdout.strip())
        except Exception:
            pass
        # Fallback defaults
        return {"horizon_years": 3}

    def _init_simulation(self) -> None:
        """Initialize the yc-bench simulation via CLI subprocess."""
        config_vals = self._load_config_values()
        horizon_years = config_vals.get("horizon_years", 3)

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

        # Read initial funds
        status = self._execute_command("yc-bench company status")
        if status["ok"]:
            try:
                data = json.loads(status["stdout"].strip())
                self.initial_funds_cents = data.get("funds_cents", 0)
            except (json.JSONDecodeError, ValueError):
                self.initial_funds_cents = 0

    def _check_terminal(self, stdout: str) -> ToolOutput | None:
        """Parse sim resume JSON for terminal conditions."""
        try:
            payload = json.loads(stdout.strip())
        except (json.JSONDecodeError, ValueError):
            return None

        terminal_reason = payload.get("terminal_reason")
        if terminal_reason in ("bankruptcy", "horizon_end"):
            return self._force_terminal(terminal_reason, extra_text=stdout)
        return None

    def _calculate_reward(self) -> float:
        """Compute reward from current simulation state."""
        status = self._execute_command("yc-bench company status")
        final_funds = 0
        try:
            data = json.loads(status.get("stdout", "{}").strip())
            final_funds = data.get("funds_cents", 0)
        except (json.JSONDecodeError, ValueError):
            pass

        if self.terminal_reason == "bankruptcy" or final_funds < 0:
            return 0.0

        if self.initial_funds_cents > 0:
            return min(1.0, max(0.0, final_funds / self.initial_funds_cents))
        return 0.0

    def _force_terminal(self, reason: str, extra_text: str = "") -> ToolOutput:
        """End the simulation and return final reward."""
        self.finished = True
        self.terminal_reason = reason
        reward = self._calculate_reward()

        text = extra_text or ""
        text += f"\n\n=== SIMULATION ENDED ===\nReason: {reason}\nReward: {reward:.4f}"

        return ToolOutput(
            blocks=[TextBlock(text=text)],
            metadata={
                "terminal_reason": reason,
                "reward": reward,
                "command_count": self.command_count,
            },
            reward=reward,
            finished=True,
        )

    async def get_prompt(self) -> List[TextBlock]:
        status = self._execute_command("yc-bench company status")
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

        # Validate
        try:
            argv = shlex.split(command)
        except ValueError:
            return ToolOutput(
                blocks=[TextBlock(text=json.dumps({"error": "Invalid command syntax"}))],
                metadata={"error": "invalid_syntax"},
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

        # Check if this was sim resume
        is_resume = len(argv) >= 3 and argv[1] == "sim" and argv[2] == "resume"

        if is_resume and exit_code == 0:
            self.commands_since_resume = 0
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
                terminal = self._check_terminal(auto_stdout)
                if terminal:
                    return terminal
                auto_resume_text = (
                    f"\n\n[AUTO-RESUME triggered after {AUTO_RESUME_THRESHOLD} "
                    f"commands without sim resume]\n{auto_stdout}"
                )

        # Build output
        output_text = stdout if exit_code == 0 else (stderr or stdout or "Command failed")
        output_text += auto_resume_text

        return ToolOutput(
            blocks=[TextBlock(text=output_text)],
            metadata={
                "exit_code": exit_code,
                "command": command,
                "command_count": self.command_count,
            },
            finished=False,
        )

    async def teardown(self) -> None:
        if self.db_dir and Path(self.db_dir).exists():
            shutil.rmtree(self.db_dir, ignore_errors=True)

    @classmethod
    def list_tasks(cls, split: str) -> list[JSONObject]:
        if split == "train":
            preset = "easy"
            seeds = [1, 2, 3]
        elif split == "test":
            preset = "default"
            seeds = [1, 2, 3]
        else:
            raise ValueError(f"Unknown split: {split}")

        return [
            {"id": f"{preset}_{seed}", "preset": preset, "seed": seed}
            for seed in seeds
        ]

    @classmethod
    def list_splits(cls) -> list[str]:
        return ["train", "test"]
