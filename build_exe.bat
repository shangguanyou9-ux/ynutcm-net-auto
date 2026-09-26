@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo === 校园网自动连接 打包脚本 ===
echo.

where python >nul 2>nul || (echo [错误] 找不到 python，请先安装并加入 PATH & pause & exit /b 1)

python -c "import PyInstaller" 2>nul || (
    echo [1/3] 未检测到 PyInstaller，正在安装...
    python -m pip install pyinstaller || (echo [错误] 安装失败 & pause & exit /b 1)
)

echo [2/3] 生成图标...
python make_icon.py || (echo [错误] 图标生成失败 & pause & exit /b 1)

echo [3/3] 开始打包（首次约 1-2 分钟）...
set HERE=%~dp0
rem 打进去的是脱敏模板，避免把你的账号密码编译进 exe
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "校园网自动连接" ^
    --icon "%HERE%app.ico" ^
    --add-data "%HERE%config.example.json;." ^
    --distpath "%HERE%dist" --workpath "%HERE%build" --specpath "%HERE%build" ^
    "%HERE%gui.py"
if errorlevel 1 (echo [错误] 打包失败 & pause & exit /b 1)

if not exist "%HERE%dist\config.json" copy /y "%HERE%config.example.json" "%HERE%dist\config.json" >nul
rd /s /q "%HERE%build" 2>nul

echo.
echo 打包完成：%HERE%dist\校园网自动连接.exe
echo 把 dist 目录整个拷走，exe 和 config.json 放同一个文件夹即可使用。
echo 首次运行请在界面上填学号和密码，然后点“保存配置”。
pause
