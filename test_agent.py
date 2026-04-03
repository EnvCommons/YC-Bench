import json
import asyncio
import os
from pathlib import Path

from openai import AsyncOpenAI
from openreward import OpenReward

TRAJECTORY_FILE = "trajectory.jsonl"


async def main():
    or_client = OpenReward()
    oai_client = AsyncOpenAI()

    MODEL_NAME = "gpt-4.1"
    ENV_NAME = "ycbench"
    SPLIT = "train"

    environment = or_client.environments.get(name=ENV_NAME, base_url="http://localhost:8080")
    tasks = environment.list_tasks(split=SPLIT)
    tools = environment.list_tools(format="openai")

    print(f"Found {len(tasks)} tasks")
    print(f"Tools: {[t['name'] for t in tools]}")

    # Clear trajectory file
    trajectory_path = Path(TRAJECTORY_FILE)
    if trajectory_path.exists():
        trajectory_path.unlink()

    # Test with first task only
    for task in tasks[:1]:
        print(f"\n{'='*60}")
        print(f"Task: {task.task_spec}")
        print(f"{'='*60}\n")

        rollout = or_client.rollout.create(
            run_name=ENV_NAME.split("/")[-1] + "_test",
            rollout_name="test_run",
            environment=ENV_NAME,
            split=SPLIT,
            task_spec=task.task_spec,
        )

        with environment.session(task=task) as session:
            prompt = session.get_prompt()
            input_list = [{"role": "user", "content": prompt[0].text}]
            finished = False
            turn = 0

            rollout.log_openai_response(message=input_list[0], is_finished=finished)

            while not finished:
                turn += 1
                try:
                    response = await oai_client.responses.create(
                        model=MODEL_NAME,
                        tools=tools,
                        input=input_list,
                    )
                except Exception as e:
                    print(f"API error on turn {turn}: {e}")
                    break

                rollout.log_openai_response(response.output[-1])
                input_list += response.output

                tool_called = False
                for item in response.output:
                    if item.type == "function_call":
                        tool_called = True
                        args = json.loads(str(item.arguments))
                        tool_result = session.call_tool(item.name, args)

                        reward = tool_result.reward
                        finished = tool_result.finished

                        output_text = tool_result.blocks[0].text if tool_result.blocks else ""

                        input_list.append({
                            "type": "function_call_output",
                            "call_id": item.call_id,
                            "output": output_text,
                        })
                        rollout.log_openai_response(
                            input_list[-1], reward=reward, is_finished=finished
                        )

                        # Log trajectory
                        entry = {
                            "turn": turn,
                            "tool": item.name,
                            "command": args.get("command", ""),
                            "output_preview": output_text[:500],
                            "reward": reward,
                            "finished": finished,
                        }
                        with open(TRAJECTORY_FILE, "a") as f:
                            f.write(json.dumps(entry) + "\n")

                        cmd = args.get("command", "")
                        print(f"[Turn {turn}] {cmd}")
                        if reward and reward != 0:
                            print(f"  Reward: {reward:.4f}")

                        if finished:
                            print(f"\n{'='*60}")
                            print(f"FINISHED! Reason: {tool_result.metadata}")
                            print(f"Final reward: {reward:.4f}")
                            print(f"Total turns: {turn}")
                            print(f"{'='*60}")
                            break

                if not tool_called and not finished:
                    # Agent responded with text, not a tool call - add it and continue
                    text_output = ""
                    for item in response.output:
                        if hasattr(item, "text"):
                            text_output += item.text
                    if text_output:
                        print(f"[Turn {turn}] Agent text: {text_output[:200]}")

    print(f"\nTrajectory saved to {TRAJECTORY_FILE}")
    print(f"Total entries: {sum(1 for _ in open(TRAJECTORY_FILE))}")


if __name__ == "__main__":
    asyncio.run(main())
