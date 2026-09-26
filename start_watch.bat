@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在以守护模式运行校园网自动登录，掉线会自动重连；关闭本窗口即停止。
echo.
python campus_net.py --watch
echo.
echo 脚本已退出。
pause
