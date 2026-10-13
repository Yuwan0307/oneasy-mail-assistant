#!/bin/bash
set -euo pipefail
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
PACKAGE="$SOURCE_ROOT/vendor/python-3.13.16-macos11.pkg"
EXPECTED_SHA=30666509020b4da0dd8bc2e773255f34d76b7bb80b66960a928d5f6daa0192d7

if [[ "$(uname -s)" != Darwin ]]; then
    echo '此部署入口用于 Mac；Windows 请双击 部署邮件助手.cmd。' >&2
    exit 1
fi
VERSION="$(sw_vers -productVersion)"
if [[ "${VERSION%%.*}" -lt 12 ]]; then
    echo '需要 macOS 12 或更新系统。' >&2
    exit 1
fi
case "$(uname -m)" in
    arm64|x86_64) ;;
    *) echo '仅支持 Apple Silicon 或 Intel Mac。' >&2; exit 1 ;;
esac
ORIGINAL_USER="$(id -un)"
if [[ "$ORIGINAL_USER" == root ]]; then
    echo '请使用自己的 Mac 账号双击部署脚本，不要以 root 账号运行。' >&2
    exit 1
fi
if [[ ! -f "$PACKAGE" ]]; then
    echo '交付包缺少 Python 运行环境，请完整解压 Mac 交付包。' >&2
    exit 1
fi
ACTUAL_SHA="$(shasum -a 256 "$PACKAGE" | awk '{print $1}')"
if [[ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]]; then
    echo 'Python 运行环境校验失败，请重新获取交付包。' >&2
    exit 1
fi

echo '正在部署 Oneasy 邮件助手。安装需要 Mac 管理员权限。'
echo '请输入 Mac 登录密码；输入密码时屏幕不会显示字符。'
sudo -v
python_is_ready() {
    [[ -x "$PYTHON" ]] && "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,13) and sys.version_info[:3] >= (3,13,16) else 1)'
}
if ! python_is_ready; then
    echo '正在安装交付包中已校验的 Python 运行环境……'
    sudo /usr/sbin/installer -pkg "$PACKAGE" -target /
fi
if ! python_is_ready; then
    echo 'Python 运行环境未正确安装，请联系管理员。' >&2
    exit 1
fi
"$PYTHON" "$SOURCE_ROOT/app/macos_deploy.py" verify "$SOURCE_ROOT"
echo '正在安装后台并保留已有配置和记录……'
sudo "$PYTHON" "$SOURCE_ROOT/app/macos_deploy.py" deploy "$SOURCE_ROOT" "$ORIGINAL_USER"
open '/Applications/Oneasy 邮件助手.app'
