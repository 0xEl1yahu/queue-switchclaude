---
description: Show or edit the queue-switchclaude task queue
argument-hint: [list | cancel pN | promote pN | park pN | drop | pause | resume [pN] | show pN | verify pN <cmd> | auto pN | manual pN | approve [pN] | help]
allowed-tools: Bash(python3 *), Bash(CLAUDE_PLUGIN_DATA=*), PowerShell(python3 *)
---
Run this exact command and print its output verbatim:

CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/queue_cli.py" $ARGUMENTS

If your shell tool is PowerShell (Windows without Git Bash), run this instead; the script finds the data directory on its own:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/queue_cli.py" $ARGUMENTS

If the output contains a line starting with "queue-switchclaude: task" that tells you to execute a plan, do what it says after printing the output.
