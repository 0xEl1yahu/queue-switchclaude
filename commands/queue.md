---
description: Show or edit the queue-switchclaude task queue
argument-hint: [list | cancel pN | promote pN | park pN | drop | pause | resume]
allowed-tools: Bash(python3 *)
---
Run this exact command and print its output verbatim, nothing else:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/queue_cli.py" $ARGUMENTS
