@echo off
setlocal
chcp 65001 >nul
echo 正在启动 Oneasy 邮件助手部署，请等待管理员授权窗口……
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy.ps1"
set "deploy_exit=%errorlevel%"
if not "%deploy_exit%"=="0" (
  echo 部署未完成，请查看上方提示。
  pause
)
exit /b %deploy_exit%
