"""Scripted stand-in for codex/claude. Usage: fake_agent.py <scenario.json> <role>

scenario[role] is a list of steps; call N uses step N (the last step repeats). A step may contain:
  files:  {relative path: content}  written into the working directory
  stdout: text to print
  exit:   exit code (default 0)
Each call also records the prompt it received in <scenario dir>/<role>-<N>.prompt.
"""

import json
import sys
from pathlib import Path

scenario_path, role = Path(sys.argv[1]), sys.argv[2]
steps = json.loads(scenario_path.read_text())[role]
counter = scenario_path.parent / f"{role}.count"
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
(scenario_path.parent / f"{role}-{n}.prompt").write_text(sys.stdin.read())

step = steps[min(n, len(steps) - 1)]
for rel, content in step.get("files", {}).items():
    target = Path.cwd() / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
print(step.get("stdout", ""))
sys.exit(step.get("exit", 0))
