#!/usr/bin/env bash
# ==============================================================================
# WorkBuddy Toolkit Uninstaller (macOS / Linux)
# 委托给跨平台的 uninstall.py，保证 models.json 还原、会话归属恢复等逻辑只有一份实现。
# 参数: --yes | --no-rollback | --keep-config
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] 需要 python3 才能卸载 WorkBuddy Toolkit。" >&2
    exit 1
fi

exec python3 "$SCRIPT_DIR/uninstall.py" "$@"
