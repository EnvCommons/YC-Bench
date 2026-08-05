# YC-Bench

[![⭐ OpenReward Environment](https://img.shields.io/badge/%E2%AD%90%20OpenReward-Environment-f7e6cc)](https://openreward.ai/collinear/YC-Bench)

## Description

YC-Bench is a long-horizon deterministic benchmark that simulates running an AI startup as CEO. The agent manages 8 employees across 4 technical domains (research, inference, data_environment, training), accepts tasks from a marketplace, assigns employees, and navigates financial and operational pressures over one simulated year. Terminal conditions are bankruptcy (funds drop below zero) or reaching the simulation horizon.

The simulation is built on a full business engine with payroll, prestige systems, client trust mechanics, adversarial clients (RATs), and multi-domain task requirements. All interactions happen through a CLI interface via the `run_command` tool.

## Capabilities

- Long-horizon strategic planning (hundreds of tool calls over simulated years)
- Resource allocation and workforce management
- Financial optimization under payroll pressure
- Client relationship management and adversarial client detection
- Multi-domain prestige progression and task gating
- Deadline management with throughput-splitting mechanics

## Compute Requirements

No sandbox or GPU required. The simulation runs as a lightweight SQLite-backed CLI tool. Minimal CPU and memory.

## License

[MIT](https://github.com/collinear-ai/yc-bench/blob/main/LICENSE) (matching the original repository).

## Tasks

There are 3 training tasks and 3 test tasks, all on the `default` preset with disjoint seeds:

- **Training:** `default_1`, `default_2`, `default_3` (seeds 1–3)
- **Test:** `default_4`, `default_5`, `default_6` (seeds 4–6)

Upstream collapsed to a single `default` preset, so the former `easy` preset no longer exists.

Each task is parameterized by a random seed that determines the employee skills, client mix, and task pool. Employees and clients are deterministic across seeds (fixed world seed), while the task marketplace varies per seed.

## Reward Structure

This is a **dense reward** environment. Reward is emitted per step as a telescoping delta of a
potential — normalized profit against the company's starting capital of $200,000:

$$\Phi_t = \max\left(-1,\ \frac{\text{funds}_t - \text{initial\_funds}}{\text{initial\_funds}}\right)
\qquad r_t = \Phi_t - \Phi_{t-1}$$

Since $\Phi_0 = 0$, the rewards telescope: **the sum of step rewards is the episode return**, equal
to profit as a fraction of starting capital. Each individual reward is simply that step's net cash
flow divided by initial funds.

| outcome | return |
|---|---|
| bankrupt (funds < 0) | −1.0 |
| break-even | 0.0 |
| +50% profit | +0.5 |
| doubled capital | +1.0 (uncapped above) |

The floor engages exactly when funds reach zero, so any bankruptcy scores −1.0 regardless of how
far past zero the final payroll pushed the balance.

Rewards are derived from the simulation's financial **ledger**, which is a complete record of every
change to company funds (`funds == initial_funds + sum(ledger)` holds at all times). Only
`sim resume` can move money, so only a resume carries a reward; all observation and task-management
commands return no reward. Per-step metadata carries `step_reward`, `episode_return`, `funds_cents`
and a signed `ledger_breakdown` by category, so the reward is fully auditable from a trajectory.

Because credit is banked as it is earned, a rollout truncated by a turn or time limit keeps the
profit it has already made rather than scoring nothing.

We do not use LLM graders. Reward is purely deterministic from simulation state.

**Consumer note:** step rewards must be **summed** to obtain the episode return. A consumer that
takes the last reward, or the maximum, will misreport it.

## Data

The simulation is self-contained. All data (employees, clients, tasks, financials) is generated deterministically from the seed and configuration preset. No external data files are needed.

## Tools

Agents have a single tool:

- **`run_command(command: str)`** — Executes any `yc-bench` CLI command. Available subcommands include:
  - `company status` — funds, prestige, payroll info
  - `employee list` — employee skills and assignments
  - `market browse` — available tasks in the marketplace
  - `task accept/assign/dispatch/cancel/inspect/list` — task lifecycle management
  - `sim resume` — advance simulation time to next event
  - `client list/history` — client trust and reliability info
  - `finance ledger` — transaction history
  - `scratchpad write/append/read` — persistent notes (survive context truncation)

All commands return JSON.

## Time Horizon

YC-Bench is a very long-horizon environment. Every task simulates 1 year of business operations, and a single episode typically involves hundreds of tool calls.

## Environment Difficulty

The `default` preset is demanding. Payroll compounds — every completed task raises the salary of
each assigned employee — so assigning everyone to everything grows costs far faster than revenue,
and payroll alone will bankrupt an idle company partway through the year. On top of that, ~35% of
clients are adversarial and inflate work requirements after acceptance, 30% of tasks are gated
behind client trust, and missing a deadline costs 35% of the advertised reward plus prestige.

Tests: employee-allocation efficiency, adversarial client detection, trust building, and cash-flow
management sustained over hundreds of turns.

## Other Environment Requirements

There are no additional secrets or API keys required. YC-Bench works out of the box with the OpenReward endpoint.

## Safety

Agents in YC-Bench interact only with a deterministic text-based simulation. There are no real-world side effects, external API calls, or web access. The simulation models a business environment where agents manage employees and finances, but all entities are fictional and decisions have no real-world impact.

## Citations

```bibtex
@article{he2026ycbench,
  title     = {YC-Bench: Benchmarking AI Agents for Long-Term Planning and Consistent Execution},
  author    = {Muyu He and Adit Jain and Anand Kumar and Vincent Tu and Soumyadeep Bakshi and Sachin Patro and Nazneen Rajani},
  year      = {2026},
  journal   = {arXiv preprint arXiv:2604.01212},
  url       = {https://arxiv.org/abs/2604.01212}
}
```
