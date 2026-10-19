---
description: Show or edit the queue-switchclaude task queue
argument-hint: [list | cancel pN | promote pN | park pN | drop | pause | resume [pN] | show pN | verify pN <cmd> | auto pN | manual pN | approve]
allowed-tools: Bash(python3 *)
---
Run this exact command and print its output verbatim:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/queue_cli.py" $ARGUMENTS

If the output contains a line starting with "queue-switchclaude: task" that tells you to execute a plan, do what it says after printing the output.
