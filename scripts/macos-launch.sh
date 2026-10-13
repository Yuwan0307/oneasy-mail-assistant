#!/bin/bash
set -euo pipefail
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
ROOT='/Library/Application Support/Oneasy/MailAssistant'
cd "$ROOT"
if [[ "$#" == 0 ]]; then
    exec "$ROOT/.venv/bin/python" -m app.macos_deploy desktop-launch
elif [[ "$#" == 1 ]]; then
    exec "$ROOT/.venv/bin/python" -m app.macos_deploy desktop-control "$1"
fi
osascript -e 'display alert "Oneasy 邮件助手" message "助手控制参数无效，请重新打开应用程序。" as warning'
exit 1
