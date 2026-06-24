#!/bin/bash
# feedback 巡检 wrapper：供 launchd 每小时调用。
# 凭证从本地 patrol.env 读（600 权限、不入 git），不写进 plist。
set -euo pipefail

ENVF="$HOME/.claude/dws-agent/patrol.env"
if [ -f "$ENVF" ]; then
  # shellcheck disable=SC1090
  source "$ENVF"
fi

cd /Users/shujudagongren/Myspace/dingding-agent
export PYTHONPATH=src
export DWS_AGENT_DWS_BIN="${DWS_AGENT_DWS_BIN:-$(command -v dws)}"

exec python3 scripts/feedback_patrol.py "$@"
