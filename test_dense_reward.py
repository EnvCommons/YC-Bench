"""Verification for YC-Bench's ledger-backed telescoping dense reward.

Runs the environment in-process — no server, no API keys:

    uv run python test_dense_reward.py

The property under test: each reward is a delta of the potential
`max(-1, (funds - initial_funds) / initial_funds)`, so the sum over an episode
equals the potential recomputed from the simulation's own balance.
"""

import json
import sys

from ycbench import POTENTIAL_FLOOR, RunCommandInput, YCBench

TASK = {"id": "default_1", "preset": "default", "seed": 1}

# Markers run_command appends after a command's own JSON output.
_SUFFIXES = ("\n\n=== SIMULATION ENDED ===", "\n\n[cash flow:", "\n\n[AUTO-RESUME")


def make_env(task=None) -> YCBench:
    return YCBench(task_spec=task or TASK)


def run(env, command):
    """Run a command, returning (ToolOutput, parsed JSON or None)."""
    out = env.run_command(RunCommandInput(command=command))
    text = out.blocks[0].text
    cuts = [text.index(s) for s in _SUFFIXES if s in text]
    if cuts:
        text = text[: min(cuts)]
    try:
        return out, json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return out, None


def play(env, employees: str, pick, max_steps: int = 4000, on_reward=None) -> list:
    """Drive a rollout to terminal, returning every ToolOutput in order.

    Keeps one task in flight and resumes; `pick` chooses from the market list.
    `on_reward` is called with each ToolOutput that carries a reward.
    """
    outs = []

    def step(command):
        out, data = run(env, command)
        outs.append(out)
        if out.reward is not None and on_reward is not None:
            on_reward(out)
        return data

    while not env.finished and len(outs) < max_steps:
        status = step("yc-bench company status")
        if env.finished:
            break
        if status and status["tasks"]["active"] == 0:
            market = step("yc-bench market browse --limit 25")
            candidates = pick((market or {}).get("tasks", []))
            dispatched = False
            for task in candidates:
                task_id = task["task_id"]
                accepted = step(f"yc-bench task accept --task-id {task_id}")
                if not accepted or "error" in accepted:
                    continue
                step(f"yc-bench task assign --task-id {task_id} --employees {employees}")
                result = step(f"yc-bench task dispatch --task-id {task_id}")
                if result and "error" not in result:
                    dispatched = True
                    break
            if not dispatched:
                break
        if env.finished:
            break
        step("yc-bench sim resume")

    return outs


def by_reward(tasks):
    """Highest-paying tasks first."""
    return sorted(tasks, key=lambda t: -t["reward_funds_cents"])


def by_workload(tasks):
    """Largest tasks first — the slowest possible progress."""
    return sorted(
        tasks,
        key=lambda t: -sum(r["required_qty"] for r in t["requirements"]),
    )


def rewards_of(outs) -> list[float]:
    return [o.reward for o in outs if o.reward is not None]


def expected_return(env) -> float:
    """The potential recomputed from the simulation's own balance."""
    funds = env._read_funds_cents()
    assert funds is not None, "could not read final funds"
    return max(POTENTIAL_FLOOR, (funds - env.initial_funds_cents) / env.initial_funds_cents)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def test_potential_is_normalized_profit():
    """Phi = max(-1.0, (funds - initial_funds) / initial_funds)"""
    env = make_env()
    initial = env.initial_funds_cents
    assert initial == 20_000_000, f"default preset should start at $200,000, got {initial}"
    assert env._potential_for(initial) == 0.0
    assert env._potential_for(initial * 2) == 1.0
    assert env._potential_for(initial // 2) == -0.5
    # Any negative balance is total loss, however far past zero it went.
    assert env._potential_for(0) == POTENTIAL_FLOOR
    assert env._potential_for(-50 * initial) == POTENTIAL_FLOOR


def test_inert_commands_emit_no_reward():
    """Only `sim resume` moves money, so nothing else may carry a reward."""
    env = make_env()
    for command in (
        "yc-bench company status",
        "yc-bench market browse --limit 5",
        "yc-bench employee list",
        "yc-bench client list",
        "yc-bench finance ledger",
        'yc-bench scratchpad write --content "notes"',
    ):
        out, _ = run(env, command)
        assert out.reward is None, f"{command} emitted reward={out.reward}"
    assert env.potential == 0.0


def test_telescoping_invariant():
    """At every step, the summed rewards equal the true potential.

    Checked per step, not just at terminal: a bankrupt rollout lands on the
    -1.0 floor, where a terminal-only check passes whether or not it telescopes.
    """
    env = make_env()
    running = 0.0
    checked_off_floor = 0

    def verify(out):
        nonlocal running, checked_off_floor
        running += out.reward
        expected = expected_return(env)
        assert abs(running - expected) < 1e-9, (
            f"telescoping broke after {len(rewards)} rewards: "
            f"running={running!r} != potential={expected!r}"
        )
        assert abs(out.metadata["episode_return"] - running) < 1e-9
        rewards.append(out.reward)
        if expected > POTENTIAL_FLOOR:
            checked_off_floor += 1

    rewards: list[float] = []
    outs = play(env, employees="Emp_1,Emp_4,Emp_7", pick=by_reward, on_reward=verify)

    assert env.finished, f"rollout did not reach terminal in {len(outs)} commands"
    assert len(rewards) > 1, "expected multiple rewarded steps"
    assert checked_off_floor > 1, (
        f"only {checked_off_floor} step(s) verified above the potential floor — "
        "the invariant was not meaningfully exercised"
    )

    total = sum(rewards)
    assert abs(total - expected_return(env)) < 1e-9
    assert abs(outs[-1].metadata["episode_return"] - total) < 1e-9
    assert outs[-1].finished is True
    print(
        f"    terminal={env.terminal_reason} commands={len(outs)} "
        f"rewarded_steps={len(rewards)} ({checked_off_floor} off-floor) return={total:+.4f}"
    )
    return env, outs


def test_ledger_matches_engine_balance_delta(outs):
    """Every step's ledger delta agrees with the engine's own balance_delta."""
    mismatches = [
        o.metadata["ledger_balance_delta_mismatch"]
        for o in outs
        if "ledger_balance_delta_mismatch" in (o.metadata or {})
    ]
    assert not mismatches, f"ledger disagreed with engine on {len(mismatches)} steps: {mismatches[:3]}"

    failures = [o for o in outs if (o.metadata or {}).get("ledger_read_failed")]
    assert not failures, f"{len(failures)} ledger reads failed"


def test_terminal_emits_residual_not_total(outs):
    """A cumulative emission at terminal would double-count against the sum."""
    terminal = outs[-1]
    total = terminal.metadata["episode_return"]
    assert terminal.reward is not None
    assert abs(terminal.reward - total) > 1e-9 or abs(total) < 1e-9, (
        f"terminal reward {terminal.reward!r} looks like the cumulative return {total!r}"
    )


def test_cash_flow_attribution(env, outs):
    """Ledger breakdowns account for the whole change in funds, signed correctly."""
    seen: dict[str, int] = {}
    for out in outs:
        for category, amount in (out.metadata or {}).get("ledger_breakdown", {}).items():
            seen[category] = seen.get(category, 0) + amount

    assert "monthly_payroll" in seen, f"no payroll recorded, categories={list(seen)}"
    assert seen["monthly_payroll"] < 0, "payroll must be an outflow"
    for category in ("task_reward", "task_fail_penalty"):
        if category in seen:
            sign = 1 if category == "task_reward" else -1
            assert seen[category] * sign > 0, f"{category} has the wrong sign"

    assert sum(seen.values()) == env._read_funds_cents() - env.initial_funds_cents, (
        "breakdown does not account for the full change in funds"
    )
    print(f"    attribution={ {k: round(v / 100) for k, v in seen.items()} } (dollars)")


def test_bankruptcy_floors_at_minus_one():
    """Any negative balance is total loss: the return is exactly -1.0."""
    env = make_env()
    outs = play(env, employees="Emp_1", pick=by_workload)
    step_rewards = rewards_of(outs)

    assert env.terminal_reason == "bankruptcy", (
        f"expected bankruptcy, got {env.terminal_reason!r} after {len(outs)} commands"
    )
    total = sum(step_rewards)
    assert abs(total - POTENTIAL_FLOOR) < 1e-9, f"bankrupt return {total!r} != {POTENTIAL_FLOOR}"

    funds = env._read_funds_cents()
    assert funds < 0, f"bankruptcy without negative funds: {funds}"
    print(f"    bankrupt at funds={funds}c after {len(outs)} commands, return={total:+.4f}")


def test_truncation_banks_partial_credit():
    """A rollout cut short keeps the profit it has already earned."""
    env = make_env()
    outs = []
    market = run(env, "yc-bench market browse --limit 5")[1]
    task_id = market["tasks"][0]["task_id"]
    for command in (
        f"yc-bench task accept --task-id {task_id}",
        f"yc-bench task assign --task-id {task_id} --employees Emp_1,Emp_4,Emp_7",
        f"yc-bench task dispatch --task-id {task_id}",
    ):
        outs.append(run(env, command)[0])
    for _ in range(6):
        outs.append(run(env, "yc-bench sim resume")[0])
        if env.finished:
            break

    assert not env.finished, "rollout ended; not testing truncation"
    step_rewards = rewards_of(outs)
    assert step_rewards, "a truncated rollout must still have banked rewards"
    total = sum(step_rewards)
    assert total > POTENTIAL_FLOOR, "expected a live, unfloored partial return"
    assert abs(total - expected_return(env)) < 1e-9
    print(f"    truncated after {len(outs)} commands with banked return={total:+.4f}")


def main() -> int:
    failures = []

    def check(name, fn, *args):
        try:
            print(f"  {name} ...")
            result = fn(*args)
            print(f"  PASS {name}")
            return result
        except AssertionError as exc:
            print(f"  FAIL {name}: {exc}")
            failures.append(name)
        except Exception as exc:  # noqa: BLE001 - surface any error as a failure
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
            failures.append(name)
        return None

    print("YC-Bench dense reward verification\n")
    check("potential is normalized profit", test_potential_is_normalized_profit)
    check("inert commands emit no reward", test_inert_commands_emit_no_reward)

    played = check("telescoping invariant", test_telescoping_invariant)
    if played:
        env, outs = played
        check("ledger matches engine balance_delta", test_ledger_matches_engine_balance_delta, outs)
        check("terminal emits residual not total", test_terminal_emits_residual_not_total, outs)
        check("cash flow attribution", test_cash_flow_attribution, env, outs)

    check("bankruptcy floors at -1.0", test_bankruptcy_floors_at_minus_one)
    check("truncation banks partial credit", test_truncation_banks_partial_credit)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
