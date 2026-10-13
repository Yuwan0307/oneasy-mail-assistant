#!/bin/bash
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
/bin/bash "$ROOT/scripts/macos-install.sh"
RESULT=$?
if [[ "$RESULT" -ne 0 ]]; then
    echo
    echo '部署未完成，请查看上方提示。'
    read -r -p '按回车键关闭此窗口。' _
fi
exit "$RESULT"
