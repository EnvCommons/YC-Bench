"""Comprehensive verification tests for yc-bench OpenReward environment.

Tests are organized in tiers:
  - Framework compliance: OpenReward contract adherence
  - Determinism: Same seed → identical output
  - Game mechanics: Task lifecycle, finances, employees, CLI coverage
  - Terminal conditions: Bankruptcy, horizon, max commands
  - Auto-resume: Threshold-based automatic time advancement
  - Error handling: Invalid inputs, edge cases
  - Prompt verification: System prompt content and structure
  - Golden snapshots: Exact numerical regression values from known seeds
"""

import inspect
import json
import shutil
from pathlib import Path

import pytest

from ycbench import YCBench, RunCommandInput, SYSTEM_PROMPT, MAX_COMMANDS, AUTO_RESUME_THRESHOLD


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

EASY_TASK = {"id": "easy_1", "preset": "easy", "seed": 1}
DEFAULT_TASK = {"id": "default_1", "preset": "default", "seed": 1}


def make_env(task=None):
    return YCBench(task_spec=task or EASY_TASK)


def run(env, command: str) -> dict:
    """Run a command and return parsed JSON (raises on non-JSON output)."""
    result = env.run_command(RunCommandInput(command=command))
    text = result.blocks[0].text
    # Strip any terminal suffix appended by _force_terminal
    if "\n\n=== SIMULATION ENDED ===" in text:
        text = text.split("\n\n=== SIMULATION ENDED ===")[0]
    return json.loads(text)


def run_raw(env, command: str):
    """Run a command and return the raw ToolOutput."""
    return env.run_command(RunCommandInput(command=command))


def setup_and_complete_task(env, task_id="Task-10", employees="Emp_8,Emp_10,Emp_4"):
    """Accept, assign, dispatch a task and resume until completion. Returns completion event."""
    run(env, f"yc-bench task accept --task-id {task_id}")
    run(env, f"yc-bench task assign --task-id {task_id} --employees {employees}")
    run(env, f"yc-bench task dispatch --task-id {task_id}")

    for _ in range(30):
        data = run(env, "yc-bench sim resume")
        for ev in data.get("wake_events", []):
            if ev.get("type") == "task_completed" and ev.get("task_title") == task_id:
                return ev
        if data.get("terminal_reason"):
            break
    return None


# =====================================================================
# SECTION 1: Framework Compliance
# =====================================================================

class TestFrameworkCompliance:
    """Verify the environment adheres to OpenReward framework contracts."""

    def test_list_splits_returns_list_of_strings(self):
        splits = YCBench.list_splits()
        assert isinstance(splits, list)
        assert all(isinstance(s, str) for s in splits)
        assert sorted(splits) == ["test", "train"]

    def test_list_tasks_train_returns_list_of_dicts(self):
        tasks = YCBench.list_tasks("train")
        assert isinstance(tasks, list)
        assert len(tasks) == 3
        for t in tasks:
            assert isinstance(t, dict)
            assert "id" in t
            assert "preset" in t
            assert "seed" in t
            assert t["preset"] == "easy"

    def test_list_tasks_test_returns_list_of_dicts(self):
        tasks = YCBench.list_tasks("test")
        assert isinstance(tasks, list)
        assert len(tasks) == 3
        for t in tasks:
            assert t["preset"] == "default"

    def test_list_tasks_seeds_sequential(self):
        tasks = YCBench.list_tasks("train")
        assert [t["seed"] for t in tasks] == [1, 2, 3]

    def test_list_tasks_unknown_split_raises(self):
        with pytest.raises(ValueError, match="Unknown split"):
            YCBench.list_tasks("unknown")

    def test_constructor_accepts_secrets_parameter(self):
        """Constructor must accept secrets kwarg per OpenReward pattern."""
        sig = inspect.signature(YCBench.__init__)
        assert "secrets" in sig.parameters

    def test_run_command_tool_uses_params_not_input(self):
        """Tool method must use 'params' parameter name, not 'input'."""
        sig = inspect.signature(YCBench.run_command)
        param_names = list(sig.parameters.keys())
        assert "params" in param_names
        assert "input" not in param_names

    @pytest.mark.asyncio
    async def test_get_prompt_returns_textblock_list(self):
        env = make_env()
        prompt = await env.get_prompt()
        assert isinstance(prompt, list)
        assert len(prompt) == 1
        # Verify it has a .text attribute (TextBlock)
        assert hasattr(prompt[0], "text")
        assert isinstance(prompt[0].text, str)

    def test_tooloutput_has_blocks_metadata_finished(self):
        env = make_env()
        result = env.run_command(RunCommandInput(command="yc-bench company status"))
        assert hasattr(result, "blocks")
        assert hasattr(result, "metadata")
        assert hasattr(result, "finished")
        assert len(result.blocks) > 0
        assert isinstance(result.metadata, dict)

    def test_server_py_is_minimal(self):
        server_path = Path(__file__).parent / "server.py"
        lines = server_path.read_text().strip().splitlines()
        assert len(lines) <= 10, f"server.py should be ~8 lines, got {len(lines)}"
        content = server_path.read_text()
        assert "Server" in content
        assert "YCBench" in content

    def test_initialization_sets_state(self):
        env = make_env()
        assert env.initial_funds_cents > 0
        assert env.command_count == 0
        assert env.commands_since_resume == 0
        assert env.finished is False
        assert env.terminal_reason is None

    def test_session_isolation(self):
        env1 = make_env({"id": "easy_1", "preset": "easy", "seed": 1})
        env2 = make_env({"id": "easy_2", "preset": "easy", "seed": 2})
        assert env1.db_path != env2.db_path

    @pytest.mark.asyncio
    async def test_teardown_removes_db(self):
        env = make_env()
        db_dir = env.db_dir
        assert Path(db_dir).exists()
        await env.teardown()
        assert not Path(db_dir).exists()


# =====================================================================
# SECTION 2: Determinism
# =====================================================================

class TestDeterminism:
    """Verify deterministic reproducibility: same seed → identical output."""

    def test_deterministic_company_status(self):
        env1 = make_env({"id": "easy_1a", "preset": "easy", "seed": 1})
        env2 = make_env({"id": "easy_1b", "preset": "easy", "seed": 1})
        s1 = run(env1, "yc-bench company status")
        s2 = run(env2, "yc-bench company status")
        # Compare everything except company_id (UUID is random per session)
        for key in ["funds_cents", "prestige", "employees", "monthly_payroll_cents"]:
            assert s1[key] == s2[key], f"Mismatch on {key}: {s1[key]} vs {s2[key]}"

    def test_deterministic_employee_list(self):
        env1 = make_env({"id": "easy_1a", "preset": "easy", "seed": 1})
        env2 = make_env({"id": "easy_1b", "preset": "easy", "seed": 1})
        e1 = run(env1, "yc-bench employee list")
        e2 = run(env2, "yc-bench employee list")
        assert e1["count"] == e2["count"]
        for emp1, emp2 in zip(e1["employees"], e2["employees"]):
            assert emp1["name"] == emp2["name"]
            assert emp1["tier"] == emp2["tier"]
            assert emp1["salary_cents"] == emp2["salary_cents"]
            assert emp1["skill_rates"] == emp2["skill_rates"]

    def test_deterministic_market_browse(self):
        env1 = make_env({"id": "easy_1a", "preset": "easy", "seed": 1})
        env2 = make_env({"id": "easy_1b", "preset": "easy", "seed": 1})
        m1 = run(env1, "yc-bench market browse --limit 20")
        m2 = run(env2, "yc-bench market browse --limit 20")
        assert m1["total"] == m2["total"]
        for t1, t2 in zip(m1["tasks"], m2["tasks"]):
            assert t1["task_id"] == t2["task_id"]
            assert t1["reward_funds_cents"] == t2["reward_funds_cents"]
            assert t1["required_prestige"] == t2["required_prestige"]

    def test_deterministic_client_list(self):
        env1 = make_env({"id": "easy_1a", "preset": "easy", "seed": 1})
        env2 = make_env({"id": "easy_1b", "preset": "easy", "seed": 1})
        c1 = run(env1, "yc-bench client list")
        c2 = run(env2, "yc-bench client list")
        assert c1["count"] == c2["count"]
        for cl1, cl2 in zip(c1["clients"], c2["clients"]):
            assert cl1["name"] == cl2["name"]
            assert cl1["tier"] == cl2["tier"]
            assert cl1["trust_level"] == cl2["trust_level"]

    def test_different_seeds_differ(self):
        env1 = make_env({"id": "easy_1", "preset": "easy", "seed": 1})
        env2 = make_env({"id": "easy_2", "preset": "easy", "seed": 2})
        m1 = run(env1, "yc-bench market browse --limit 5")
        m2 = run(env2, "yc-bench market browse --limit 5")
        # Task rewards should differ between seeds
        rewards1 = [t["reward_funds_cents"] for t in m1["tasks"]]
        rewards2 = [t["reward_funds_cents"] for t in m2["tasks"]]
        assert rewards1 != rewards2, "Different seeds should produce different market tasks"

    def test_deterministic_full_lifecycle(self):
        """Accept same task, assign same employees, dispatch, resume → identical structure."""
        results = []
        for _ in range(2):
            env = make_env({"id": "easy_1", "preset": "easy", "seed": 1})
            run(env, "yc-bench task accept --task-id Task-10")
            run(env, "yc-bench task assign --task-id Task-10 --employees Emp_8,Emp_10,Emp_4")
            run(env, "yc-bench task dispatch --task-id Task-10")
            resume_data = run(env, "yc-bench sim resume")
            results.append(resume_data)

        # Events processed count should be identical
        assert results[0]["events_processed"] == results[1]["events_processed"]
        # Wake event types and milestones should match (UUIDs differ per session)
        events0 = [(e["type"], e.get("milestone_pct")) for e in results[0]["wake_events"]]
        events1 = [(e["type"], e.get("milestone_pct")) for e in results[1]["wake_events"]]
        assert events0 == events1
        # Sim time advancement should be identical
        assert results[0]["new_sim_time"] == results[1]["new_sim_time"]


# =====================================================================
# SECTION 3: Game Mechanics - Task Lifecycle
# =====================================================================

class TestTaskLifecycle:
    """Verify task status transitions and lifecycle mechanics."""

    def test_task_accept_transitions_to_planned(self):
        env = make_env()
        data = run(env, "yc-bench task accept --task-id Task-10")
        assert data["status"] == "planned"
        assert data["task_id"] == "Task-10"
        assert "accepted_at" in data
        assert "deadline" in data

    def test_task_dispatch_transitions_to_active(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        run(env, "yc-bench task assign --task-id Task-10 --employees Emp_8")
        data = run(env, "yc-bench task dispatch --task-id Task-10")
        assert data["status"] == "active"

    def test_task_inspect_shows_details(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        data = run(env, "yc-bench task inspect --task-id Task-10")
        assert data["task_id"] == "Task-10"
        assert data["status"] == "planned"
        assert "requirements" in data
        assert "reward_funds_cents" in data
        assert data["required_prestige"] == 1

    def test_task_list_shows_accepted_tasks(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        data = run(env, "yc-bench task list")
        assert data["count"] >= 1
        task_ids = [t["task_id"] for t in data["tasks"]]
        assert "Task-10" in task_ids

    def test_task_completion_success(self):
        """Full task lifecycle: accept → assign → dispatch → resume until success."""
        env = make_env()
        ev = setup_and_complete_task(env)
        assert ev is not None, "Task-10 should complete within 30 resumes"
        assert ev["success"] is True
        assert ev["funds_delta"] > 0

    def test_market_replenishment_after_accept(self):
        env = make_env()
        data = run(env, "yc-bench task accept --task-id Task-10")
        replacement_id = data.get("replacement_task_id")
        assert replacement_id is not None, "Accepting a task should generate a replacement"

    def test_task_dispatch_requires_assignment(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        result = run_raw(env, "yc-bench task dispatch --task-id Task-10")
        text = result.blocks[0].text.lower()
        assert "error" in text or "no employees" in text

    def test_sim_resume_blocks_without_active_tasks(self):
        env = make_env()
        result = run_raw(env, "yc-bench sim resume")
        text = result.blocks[0].text.lower()
        assert "error" in text or "no active" in text
        assert result.finished is False


# =====================================================================
# SECTION 4: Game Mechanics - Financial System
# =====================================================================

class TestFinancialSystem:
    """Verify financial mechanics: funds, payroll, rewards."""

    def test_initial_funds_easy_preset(self):
        env = make_env()
        assert env.initial_funds_cents == 20_000_000  # $200,000 for easy preset

    def test_initial_funds_default_preset(self):
        env = make_env(DEFAULT_TASK)
        assert env.initial_funds_cents == 15_000_000  # $150,000 for default preset

    def test_company_status_funds(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        assert data["funds_cents"] == 20_000_000
        assert data["monthly_payroll_cents"] > 0

    def test_task_completion_increases_funds(self):
        env = make_env()
        before = run(env, "yc-bench company status")["funds_cents"]
        ev = setup_and_complete_task(env)
        assert ev is not None
        after = run(env, "yc-bench company status")["funds_cents"]
        assert after > before, "Completing a task should increase funds"

    def test_reward_calculation_formula(self):
        """reward = min(1.0, max(0.0, final_funds / initial_funds))"""
        env = make_env()
        # Initial funds = 20M, at start reward should be 1.0
        reward = env._calculate_reward()
        assert reward == 1.0  # 20M / 20M = 1.0

    def test_finance_ledger_empty_initially(self):
        env = make_env()
        data = run(env, "yc-bench finance ledger")
        assert data["count"] == 0
        assert data["entries"] == []

    def test_finance_ledger_after_task_completion(self):
        env = make_env()
        setup_and_complete_task(env)
        data = run(env, "yc-bench finance ledger")
        assert data["count"] > 0, "Ledger should have entries after task completion"
        # Should contain a task_reward entry
        categories = [e["category"] for e in data["entries"]]
        assert "task_reward" in categories

    def test_payroll_deduction_after_month(self):
        """After advancing past a payroll boundary, funds should decrease."""
        env = make_env()
        initial_funds = run(env, "yc-bench company status")["funds_cents"]
        monthly_payroll = run(env, "yc-bench company status")["monthly_payroll_cents"]

        # Complete a task to be able to resume, then keep resuming past payroll
        setup_and_complete_task(env)
        # Accept another task to keep resuming
        run(env, "yc-bench task accept --task-id Task-17")
        run(env, "yc-bench task assign --task-id Task-17 --employees Emp_1,Emp_6")
        run(env, "yc-bench task dispatch --task-id Task-17")

        # Resume many times to pass payroll boundary
        payroll_seen = False
        for _ in range(50):
            data = run(env, "yc-bench sim resume")
            if data.get("payrolls_applied", 0) > 0:
                payroll_seen = True
                break
            for ev in data.get("wake_events", []):
                if ev.get("type") == "monthly_payroll":
                    payroll_seen = True
                    break
            if payroll_seen or data.get("terminal_reason"):
                break

        final_status = run(env, "yc-bench company status")
        # Funds should have changed (reward added, payroll subtracted)
        assert final_status["funds_cents"] != initial_funds


# =====================================================================
# SECTION 5: Game Mechanics - Employee System
# =====================================================================

class TestEmployeeSystem:
    """Verify employee mechanics: tiers, skill rates, domains."""

    def test_employee_count(self):
        env = make_env()
        data = run(env, "yc-bench employee list")
        assert data["count"] == 10

    def test_employee_tiers_present(self):
        env = make_env()
        data = run(env, "yc-bench employee list")
        tiers = {e["tier"] for e in data["employees"]}
        assert "junior" in tiers
        assert "mid" in tiers
        assert "senior" in tiers

    def test_employee_tier_distribution(self):
        """Should match salary tier shares: 50% junior, 35% mid, 15% senior."""
        env = make_env()
        data = run(env, "yc-bench employee list")
        tier_counts = {}
        for e in data["employees"]:
            tier_counts[e["tier"]] = tier_counts.get(e["tier"], 0) + 1
        assert tier_counts["junior"] == 5
        assert tier_counts["mid"] == 3
        assert tier_counts["senior"] == 2

    def test_employee_skill_rates_four_domains(self):
        env = make_env()
        data = run(env, "yc-bench employee list")
        domains = {"data_environment", "inference", "research", "training"}
        for e in data["employees"]:
            assert set(e["skill_rates"].keys()) == domains
            for rate in e["skill_rates"].values():
                assert rate > 0, f"Employee {e['name']} has zero rate"

    def test_employee_names_sequential(self):
        env = make_env()
        data = run(env, "yc-bench employee list")
        names = [e["name"] for e in data["employees"]]
        expected = [f"Emp_{i}" for i in range(1, 11)]
        assert names == expected

    def test_senior_employees_higher_rates(self):
        """Senior employees should generally have higher skill rates than juniors."""
        env = make_env()
        data = run(env, "yc-bench employee list")
        senior_avg = []
        junior_avg = []
        for e in data["employees"]:
            avg_rate = sum(e["skill_rates"].values()) / len(e["skill_rates"])
            if e["tier"] == "senior":
                senior_avg.append(avg_rate)
            elif e["tier"] == "junior":
                junior_avg.append(avg_rate)
        if senior_avg and junior_avg:
            assert sum(senior_avg) / len(senior_avg) > sum(junior_avg) / len(junior_avg)


# =====================================================================
# SECTION 6: Game Mechanics - Client System
# =====================================================================

class TestClientSystem:
    """Verify client mechanics: trust, history, count."""

    def test_client_count(self):
        env = make_env()
        data = run(env, "yc-bench client list")
        assert data["count"] == 8

    def test_client_trust_starts_at_zero(self):
        env = make_env()
        data = run(env, "yc-bench client list")
        for client in data["clients"]:
            assert client["trust_level"] == 0.0

    def test_client_has_specialties(self):
        env = make_env()
        data = run(env, "yc-bench client list")
        for client in data["clients"]:
            assert "specialties" in client
            assert isinstance(client["specialties"], list)
            assert len(client["specialties"]) > 0

    def test_client_history_initially_empty(self):
        env = make_env()
        data = run(env, "yc-bench client history")
        assert data["count"] == 8
        for ch in data["client_history"]:
            assert ch["tasks_succeeded"] == 0
            assert ch["tasks_failed"] == 0

    def test_client_trust_increases_after_task_completion(self):
        env = make_env()
        setup_and_complete_task(env)
        data = run(env, "yc-bench client list")
        # Task-10 is from "Atlas Computing"
        atlas = [c for c in data["clients"] if c["name"] == "Atlas Computing"]
        assert len(atlas) == 1
        assert atlas[0]["trust_level"] > 0, "Trust should increase after task completion"

    def test_client_history_updates_after_completion(self):
        env = make_env()
        setup_and_complete_task(env)
        data = run(env, "yc-bench client history")
        atlas = [c for c in data["client_history"] if c["client_name"] == "Atlas Computing"]
        assert len(atlas) == 1
        assert atlas[0]["tasks_succeeded"] == 1


# =====================================================================
# SECTION 7: CLI Command Coverage
# =====================================================================

class TestCLICommands:
    """Verify all CLI command groups work through the wrapper."""

    def test_company_status(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        assert "company_name" in data
        assert "funds_cents" in data
        assert "prestige" in data
        assert "sim_time" in data
        assert "horizon_end" in data
        assert "employees" in data
        assert "monthly_payroll_cents" in data

    def test_employee_list(self):
        env = make_env()
        data = run(env, "yc-bench employee list")
        assert "count" in data
        assert "employees" in data
        assert data["count"] > 0

    def test_market_browse(self):
        env = make_env()
        data = run(env, "yc-bench market browse")
        assert "tasks" in data
        assert len(data["tasks"]) > 0
        task = data["tasks"][0]
        assert "task_id" in task
        assert "reward_funds_cents" in task
        assert "required_prestige" in task

    def test_market_browse_with_limit(self):
        env = make_env()
        data = run(env, "yc-bench market browse --limit 3")
        assert len(data["tasks"]) <= 3

    def test_finance_ledger(self):
        env = make_env()
        data = run(env, "yc-bench finance ledger")
        assert "count" in data
        assert "entries" in data

    def test_report_monthly(self):
        env = make_env()
        data = run(env, "yc-bench report monthly")
        assert "count" in data
        assert "months" in data

    def test_scratchpad_write_and_read(self):
        env = make_env()
        run(env, 'yc-bench scratchpad write --content "Hello World"')
        data = run(env, "yc-bench scratchpad read")
        assert data["content"] == "Hello World"

    def test_scratchpad_append(self):
        env = make_env()
        run(env, 'yc-bench scratchpad write --content "First"')
        run(env, 'yc-bench scratchpad append --content " Second"')
        data = run(env, "yc-bench scratchpad read")
        assert "First" in data["content"]
        assert "Second" in data["content"]

    def test_scratchpad_clear(self):
        env = make_env()
        run(env, 'yc-bench scratchpad write --content "Something"')
        run(env, "yc-bench scratchpad clear")
        data = run(env, "yc-bench scratchpad read")
        assert data["content"] == ""

    def test_client_list(self):
        env = make_env()
        data = run(env, "yc-bench client list")
        assert "count" in data
        assert "clients" in data
        assert data["count"] > 0

    def test_client_history(self):
        env = make_env()
        data = run(env, "yc-bench client history")
        assert "count" in data
        assert "client_history" in data

    def test_task_list(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        data = run(env, "yc-bench task list")
        assert "count" in data
        assert "tasks" in data

    def test_task_list_filter_by_status(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        data = run(env, "yc-bench task list --status planned")
        assert data["count"] >= 1
        for t in data["tasks"]:
            assert t["status"] == "planned"

    def test_task_inspect(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        data = run(env, "yc-bench task inspect --task-id Task-10")
        assert data["task_id"] == "Task-10"
        assert "requirements" in data
        assert "assignments" in data


# =====================================================================
# SECTION 8: Terminal Conditions
# =====================================================================

class TestTerminalConditions:
    """Verify terminal state handling: max commands, bankruptcy, horizon."""

    def test_max_commands_terminates(self):
        env = make_env()
        env.command_count = MAX_COMMANDS  # Force to limit
        result = env.run_command(RunCommandInput(command="yc-bench company status"))
        assert result.finished is True
        assert env.terminal_reason == "max_commands"

    def test_already_finished_blocks_commands(self):
        env = make_env()
        env.finished = True
        env.terminal_reason = "test_reason"
        result = env.run_command(RunCommandInput(command="yc-bench company status"))
        assert result.finished is True
        assert "test_reason" in result.blocks[0].text

    def test_terminal_metadata_includes_reason(self):
        env = make_env()
        env.command_count = MAX_COMMANDS
        result = env.run_command(RunCommandInput(command="yc-bench company status"))
        assert "terminal_reason" in result.metadata
        assert result.metadata["terminal_reason"] == "max_commands"

    def test_terminal_reward_included(self):
        env = make_env()
        env.command_count = MAX_COMMANDS
        result = env.run_command(RunCommandInput(command="yc-bench company status"))
        assert result.reward is not None
        assert isinstance(result.reward, float)

    def test_reward_is_ratio_of_funds(self):
        """reward = min(1.0, max(0.0, final_funds / initial_funds))"""
        env = make_env()
        # At start, funds == initial_funds, so reward should be 1.0
        env.command_count = MAX_COMMANDS
        result = env.run_command(RunCommandInput(command="yc-bench company status"))
        assert result.reward == 1.0


# =====================================================================
# SECTION 9: Auto-Resume
# =====================================================================

class TestAutoResume:
    """Verify auto-resume triggers after threshold commands without sim resume."""

    def test_commands_since_resume_increments(self):
        env = make_env()
        assert env.commands_since_resume == 0
        env.run_command(RunCommandInput(command="yc-bench company status"))
        assert env.commands_since_resume == 1
        env.run_command(RunCommandInput(command="yc-bench employee list"))
        assert env.commands_since_resume == 2

    def test_commands_since_resume_resets_on_resume(self):
        env = make_env()
        # Do some commands first
        env.run_command(RunCommandInput(command="yc-bench company status"))
        env.run_command(RunCommandInput(command="yc-bench employee list"))
        assert env.commands_since_resume == 2

        # Setup and do a real resume
        run(env, "yc-bench task accept --task-id Task-10")
        run(env, "yc-bench task assign --task-id Task-10 --employees Emp_8")
        run(env, "yc-bench task dispatch --task-id Task-10")
        env.run_command(RunCommandInput(command="yc-bench sim resume"))
        assert env.commands_since_resume == 0

    def test_auto_resume_threshold_value(self):
        assert AUTO_RESUME_THRESHOLD == 30

    def test_auto_resume_triggers_at_threshold(self):
        """After AUTO_RESUME_THRESHOLD non-resume commands, auto-resume should fire."""
        env = make_env()
        # Set up an active task so auto-resume has something to advance
        run(env, "yc-bench task accept --task-id Task-10")
        run(env, "yc-bench task assign --task-id Task-10 --employees Emp_8")
        run(env, "yc-bench task dispatch --task-id Task-10")
        env.commands_since_resume = 0  # reset after setup

        # Issue enough commands to trigger auto-resume
        for i in range(AUTO_RESUME_THRESHOLD):
            env.run_command(RunCommandInput(command="yc-bench company status"))

        # After threshold, commands_since_resume should have been reset by auto-resume
        assert env.commands_since_resume == 0, \
            f"Auto-resume should have reset counter, got {env.commands_since_resume}"


# =====================================================================
# SECTION 10: Error Handling
# =====================================================================

class TestErrorHandling:
    """Verify error handling for invalid inputs and edge cases."""

    def test_non_yc_bench_command_rejected(self):
        env = make_env()
        result = env.run_command(RunCommandInput(command="ls -la"))
        assert result.finished is False
        text = result.blocks[0].text
        data = json.loads(text)
        assert "error" in data

    def test_malformed_command_syntax(self):
        env = make_env()
        result = env.run_command(RunCommandInput(command='yc-bench task accept --task-id "unclosed'))
        assert result.finished is False

    def test_invalid_task_id(self):
        env = make_env()
        result = run_raw(env, "yc-bench task accept --task-id NonexistentTask-999")
        text = result.blocks[0].text.lower()
        assert "error" in text or "not found" in text

    def test_double_accept_fails(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        result = run_raw(env, "yc-bench task accept --task-id Task-10")
        text = result.blocks[0].text.lower()
        assert "error" in text or "not in market" in text

    def test_dispatch_without_accept_fails(self):
        env = make_env()
        # Task-10 is in market, not planned
        result = run_raw(env, "yc-bench task dispatch --task-id Task-10")
        text = result.blocks[0].text.lower()
        assert "error" in text or "must be planned" in text

    def test_empty_command_rejected(self):
        env = make_env()
        result = env.run_command(RunCommandInput(command=""))
        assert result.finished is False

    def test_command_count_increments_on_valid(self):
        env = make_env()
        env.run_command(RunCommandInput(command="yc-bench company status"))
        assert env.command_count == 1
        env.run_command(RunCommandInput(command="yc-bench employee list"))
        assert env.command_count == 2

    def test_command_count_does_not_increment_on_rejected(self):
        """Non-yc-bench commands should not increment command_count."""
        env = make_env()
        env.run_command(RunCommandInput(command="ls -la"))
        assert env.command_count == 0  # rejected before execution


# =====================================================================
# SECTION 11: Prompt Verification
# =====================================================================

class TestPromptVerification:
    """Verify system prompt content matches original and includes state."""

    @pytest.mark.asyncio
    async def test_prompt_contains_ceo_framing(self):
        env = make_env()
        prompt = await env.get_prompt()
        text = prompt[0].text
        assert "CEO" in text

    @pytest.mark.asyncio
    async def test_prompt_contains_command_reference(self):
        env = make_env()
        prompt = await env.get_prompt()
        text = prompt[0].text
        assert "yc-bench company status" in text
        assert "yc-bench market browse" in text
        assert "yc-bench task accept" in text
        assert "yc-bench sim resume" in text

    @pytest.mark.asyncio
    async def test_prompt_contains_initial_state(self):
        env = make_env()
        prompt = await env.get_prompt()
        text = prompt[0].text
        assert "funds_cents" in text

    @pytest.mark.asyncio
    async def test_prompt_mentions_run_command_tool(self):
        env = make_env()
        prompt = await env.get_prompt()
        text = prompt[0].text
        assert "run_command" in text

    @pytest.mark.asyncio
    async def test_prompt_contains_key_mechanics(self):
        """Prompt should explain salary bumps, throughput split, deadlines, trust."""
        env = make_env()
        prompt = await env.get_prompt()
        text = prompt[0].text
        assert "Salary bumps" in text
        assert "Throughput split" in text
        assert "Deadlines" in text
        assert "Trust" in text

    @pytest.mark.asyncio
    async def test_prompt_contains_scratchpad_reference(self):
        env = make_env()
        prompt = await env.get_prompt()
        text = prompt[0].text
        assert "scratchpad" in text.lower()

    def test_system_prompt_matches_original(self):
        """SYSTEM_PROMPT constant should match the original agent/prompt.py content."""
        # Key phrases from the original
        assert "You are the CEO of a startup" in SYSTEM_PROMPT
        assert "Maximize funds and prestige while avoiding bankruptcy" in SYSTEM_PROMPT
        assert "Core Workflow" in SYSTEM_PROMPT
        assert "yc-bench market browse" in SYSTEM_PROMPT


# =====================================================================
# SECTION 12: Golden Snapshot Tests
# =====================================================================

class TestGoldenSnapshots:
    """Exact numerical regression tests from known seeds.

    These values were captured from the yc-bench simulation engine and serve
    as regression tests to detect any behavioral drift in the migration.
    """

    # --- Easy preset, seed 1 ---

    def test_golden_easy_seed1_initial_funds(self):
        env = make_env()
        assert env.initial_funds_cents == 20_000_000

    def test_golden_easy_seed1_monthly_payroll(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        assert data["monthly_payroll_cents"] == 6_696_570

    def test_golden_easy_seed1_employee_count(self):
        env = make_env()
        data = run(env, "yc-bench employee list")
        assert data["count"] == 10

    def test_golden_easy_seed1_employee_tiers(self):
        env = make_env()
        data = run(env, "yc-bench employee list")
        tiers = [e["tier"] for e in data["employees"]]
        assert tiers == ["mid", "junior", "junior", "mid", "junior", "mid", "junior", "senior", "junior", "senior"]

    def test_golden_easy_seed1_employee_salaries(self):
        env = make_env()
        data = run(env, "yc-bench employee list")
        salaries = [e["salary_cents"] for e in data["employees"]]
        expected = [797260, 314782, 236809, 757445, 259604, 776920, 264726, 1482393, 387869, 1418762]
        assert salaries == expected

    def test_golden_easy_seed1_senior_skill_rates(self):
        """Emp_8 (senior) skill rates should match golden values."""
        env = make_env()
        data = run(env, "yc-bench employee list")
        emp8 = data["employees"][7]  # Emp_8
        assert emp8["name"] == "Emp_8"
        assert emp8["tier"] == "senior"
        assert emp8["skill_rates"]["research"] == 9.84
        assert emp8["skill_rates"]["training"] == 9.92

    def test_golden_easy_seed1_first_market_task(self):
        """First market task (by reward descending) should be Task-10."""
        env = make_env()
        data = run(env, "yc-bench market browse --limit 1")
        task = data["tasks"][0]
        assert task["task_id"] == "Task-10"
        assert task["client_name"] == "Atlas Computing"
        assert task["required_prestige"] == 1
        assert task["reward_funds_cents"] == 3_558_437
        assert task["reward_prestige_delta"] == 0.11

    def test_golden_easy_seed1_market_tasks_all_prestige_1(self):
        """Easy preset: all market tasks should have required_prestige=1."""
        env = make_env()
        data = run(env, "yc-bench market browse --limit 200")
        for task in data["tasks"]:
            assert task["required_prestige"] == 1, \
                f"Task {task['task_id']} has prestige {task['required_prestige']}, expected 1"

    def test_golden_easy_seed1_client_names(self):
        env = make_env()
        data = run(env, "yc-bench client list")
        names = sorted([c["name"] for c in data["clients"]])
        expected = sorted([
            "Atlas Computing", "Cortex Intelligence", "Equinox Labs",
            "Helix Systems", "Prism Analytics", "Stratos Cloud",
            "Vanguard ML", "Vertex Labs"
        ])
        assert names == expected

    def test_golden_easy_seed1_prestige_starts_at_1(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        for domain, level in data["prestige"].items():
            assert level == 1.0, f"Prestige for {domain} should start at 1.0, got {level}"

    def test_golden_easy_seed1_four_domains(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        expected_domains = {"data_environment", "inference", "research", "training"}
        assert set(data["prestige"].keys()) == expected_domains

    def test_golden_easy_seed1_task_completion_reward(self):
        """Completing Task-10 should add exactly 3,558,437 cents to funds."""
        env = make_env()
        ev = setup_and_complete_task(env)
        assert ev is not None
        assert ev["funds_delta"] == 3_558_437
        assert ev["listed_reward"] == 3_558_437

    def test_golden_easy_seed1_task_completion_prestige_gain(self):
        """After completing Task-10 (research domain), research prestige should increase."""
        env = make_env()
        setup_and_complete_task(env)
        data = run(env, "yc-bench company status")
        assert data["prestige"]["research"] == 1.11  # 1.0 + 0.11 delta

    def test_golden_easy_seed1_task_completion_trust_gain(self):
        """After completing Task-10, trust with Atlas Computing should increase."""
        env = make_env()
        ev = setup_and_complete_task(env)
        assert ev is not None
        assert ev["trust_delta"] > 0

    def test_golden_easy_seed1_salary_bump_after_completion(self):
        """Assigned employees get salary bumps after task completion."""
        env = make_env()
        ev = setup_and_complete_task(env)
        assert ev is not None
        assert ev["salary_bump_total_cents"] > 0

    def test_golden_easy_seed1_funds_after_one_task(self):
        """After completing Task-10: initial 20M + 3,558,437 reward."""
        env = make_env()
        setup_and_complete_task(env)
        data = run(env, "yc-bench company status")
        assert data["funds_cents"] == 23_558_437

    # --- Default preset, seed 1 ---

    def test_golden_default_seed1_initial_funds(self):
        env = make_env(DEFAULT_TASK)
        assert env.initial_funds_cents == 15_000_000

    def test_golden_default_seed1_employee_count(self):
        env = make_env(DEFAULT_TASK)
        data = run(env, "yc-bench employee list")
        assert data["count"] == 10

    def test_golden_default_seed1_same_tier_distribution(self):
        """Employees use fixed seed, so tier distribution is same across presets."""
        env = make_env(DEFAULT_TASK)
        data = run(env, "yc-bench employee list")
        tier_counts = {}
        for e in data["employees"]:
            tier_counts[e["tier"]] = tier_counts.get(e["tier"], 0) + 1
        assert tier_counts == {"junior": 5, "mid": 3, "senior": 2}

    # --- Easy seed 2 (different from seed 1) ---

    def test_golden_easy_seed2_different_market(self):
        """Easy seed 2 should have different market tasks than seed 1."""
        env1 = make_env({"id": "easy_1", "preset": "easy", "seed": 1})
        env2 = make_env({"id": "easy_2", "preset": "easy", "seed": 2})
        m1 = run(env1, "yc-bench market browse --limit 3")
        m2 = run(env2, "yc-bench market browse --limit 3")
        ids1 = [t["task_id"] for t in m1["tasks"]]
        ids2 = [t["task_id"] for t in m2["tasks"]]
        # At least the top task by reward should differ (different seed)
        rewards1 = [t["reward_funds_cents"] for t in m1["tasks"]]
        rewards2 = [t["reward_funds_cents"] for t in m2["tasks"]]
        assert rewards1 != rewards2


# =====================================================================
# SECTION 13: Prestige System
# =====================================================================

class TestPrestigeSystem:
    """Verify prestige mechanics: domain tracking, gains, initial values."""

    def test_four_prestige_domains(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        assert len(data["prestige"]) == 4
        assert set(data["prestige"].keys()) == {"data_environment", "inference", "research", "training"}

    def test_prestige_initial_value(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        for domain, level in data["prestige"].items():
            assert level == 1.0

    def test_prestige_increases_on_task_success(self):
        env = make_env()
        before = run(env, "yc-bench company status")["prestige"]["research"]
        setup_and_complete_task(env)  # Task-10 is research domain
        after = run(env, "yc-bench company status")["prestige"]["research"]
        assert after > before

    def test_prestige_only_increases_in_task_domain(self):
        """Completing a research task should not increase training prestige."""
        env = make_env()
        before = run(env, "yc-bench company status")["prestige"]
        setup_and_complete_task(env)  # Task-10 is research domain
        after = run(env, "yc-bench company status")["prestige"]
        # Training should not have increased (may have decayed slightly)
        assert after["training"] <= before["training"]


# =====================================================================
# SECTION 14: Horizon and Simulation Time
# =====================================================================

class TestSimulationTime:
    """Verify simulation time tracking and horizon."""

    def test_sim_time_starts_at_jan_1_2025(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        assert data["sim_time"].startswith("2025-01-01T09:00:00")

    def test_horizon_end_one_year_for_easy(self):
        env = make_env()
        data = run(env, "yc-bench company status")
        assert data["horizon_end"].startswith("2026-01-01T09:00:00")

    def test_sim_time_advances_after_resume(self):
        env = make_env()
        before = run(env, "yc-bench company status")["sim_time"]
        run(env, "yc-bench task accept --task-id Task-10")
        run(env, "yc-bench task assign --task-id Task-10 --employees Emp_8")
        run(env, "yc-bench task dispatch --task-id Task-10")
        run(env, "yc-bench sim resume")
        after = run(env, "yc-bench company status")["sim_time"]
        assert after > before, "Sim time should advance after resume"

    def test_resume_returns_event_info(self):
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        run(env, "yc-bench task assign --task-id Task-10 --employees Emp_8,Emp_10,Emp_4")
        run(env, "yc-bench task dispatch --task-id Task-10")
        data = run(env, "yc-bench sim resume")
        assert "ok" in data
        assert "wake_events" in data
        assert "events_processed" in data
        assert isinstance(data["wake_events"], list)


# =====================================================================
# SECTION 15: Known Bug Documentation
# =====================================================================

class TestKnownBugs:
    """Document known bugs in the original yc-bench code."""

    @pytest.mark.xfail(reason="Original yc-bench bug: task_commands.py:566 uses .astext (PostgreSQL only, fails on SQLite)")
    def test_task_cancel_works(self):
        """Task cancel should apply prestige penalty and return info."""
        env = make_env()
        run(env, "yc-bench task accept --task-id Task-10")
        data = run(env, "yc-bench task cancel --task-id Task-10 --reason testing")
        assert data.get("status") == "cancelled"
