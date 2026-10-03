#!/bin/bash
# Finder opens this executable in Terminal, including paths containing spaces.
TASK_ROOT="$(cd "$(dirname "$0")" && pwd)"
/bin/bash "$TASK_ROOT/scripts/bootstrap_macos.sh"
status=$?
printf '\n按回车关闭窗口。\n'
read -r unused
exit "$status"
