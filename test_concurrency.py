"""Concurrent sessions in one process must not see each other's state or output.

    uv run pytest test_concurrency.py
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from ycbench import AUTO_RESUME_THRESHOLD, RunCommandInput, YCBench


def _session(seed: int, rounds: int) -> list[str]:
    # Stay under AUTO_RESUME_THRESHOLD, which appends a suffix to the output.
    assert 1 + 2 * rounds < AUTO_RESUME_THRESHOLD
    env = YCBench(task_spec={"id": f"default_{seed}", "preset": "default", "seed": seed})
    marker = f"marker-{seed}-{threading.get_ident()}"
    env.run_command(RunCommandInput(command=f'yc-bench scratchpad write --content "{marker}"'))
    problems = []
    for _ in range(rounds):
        for command in ("yc-bench company status", "yc-bench scratchpad read"):
            text = env.run_command(RunCommandInput(command=command)).blocks[0].text
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                problems.append(f"seed {seed}: non-JSON output from {command!r}: {text[:200]!r}")
                continue
            if command.endswith("read") and data.get("content") != marker:
                problems.append(f"seed {seed}: scratchpad {data.get('content')!r} != {marker!r}")
    return problems


def test_output_and_state_isolated_across_threads():
    """Server logs written from other threads must not leak into a session's tool output."""
    stop = threading.Event()

    def chatter():
        while not stop.is_set():
            print("server log line from another thread")

    noise = threading.Thread(target=chatter, daemon=True)
    noise.start()
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda s: _session(s, rounds=12), [1, 2, 3, 4, 5, 6]))
    finally:
        stop.set()
        noise.join()

    problems = [p for r in results for p in r]
    assert not problems, "\n".join(problems[:10])
