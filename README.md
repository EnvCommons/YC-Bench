# YC-Bench

[![⭐ OpenReward Environment](https://img.shields.io/badge/%E2%AD%90%20OpenReward-Environment-f7e6cc)](https://openreward.ai/collinear/YC-Bench)

## Description

YC-Bench is a long-horizon deterministic benchmark that simulates running an AI startup as CEO. The agent manages 10 employees across 4 technical domains (research, inference, data_environment, training), accepts tasks from a marketplace, assigns employees, and navigates financial and operational pressures over 1–3 simulated years. Terminal conditions are bankruptcy (funds drop below zero) or reaching the simulation horizon.

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

There are 3 training tasks and 3 test tasks:

**Training (easy preset, 1-year horizon):**
- `easy_1`, `easy_2`, `easy_3` — Single-domain tasks, accessible prestige requirements, forgiving penalties. Tests basic throughput awareness.

**Test (default preset, 3-year horizon):**
- `default_1`, `default_2`, `default_3` — Multi-domain tasks, prestige mode=4 (most tasks need prestige 3–5), tight deadlines, costly cancellations. The canonical benchmark configuration.

Each task is parameterized by a random seed that determines the employee skills, client mix, and task pool. Employees and clients are deterministic across seeds (fixed world seed), while the task marketplace varies per seed.

## Reward Structure

This is a sparse reward environment. Reward is computed at terminal:

$$R = \begin{cases} 0 & \text{if bankrupt (funds} < 0\text{)} \\ \min\left(1, \frac{\text{final\_funds}}{\text{initial\_funds}}\right) & \text{if survived to horizon} \end{cases}$$

Initial funds are $200,000 for easy tasks and $150,000 for default tasks.

We do not use LLM graders. Reward is purely deterministic from simulation state.

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
  - `report monthly` — monthly P&L summary

All commands return JSON.

## Time Horizon

YC-Bench is a very long-horizon environment. Easy tasks simulate 1 year of business operations; default tasks simulate 3 years. A single episode typically involves hundreds of tool calls.

## Environment Difficulty

Difficulty varies by preset:

- **Easy**: Most tasks accessible at prestige 1, single-domain, forgiving penalties. Tests whether agents avoid throughput dilution from excessive parallelism.
- **Default**: Multi-domain tasks requiring prestige 3–5, tight deadlines, 1.4x failure penalties, 2.0x cancellation penalties. Tests sustained strategic decision-making over 3 years.

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
