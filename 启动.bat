@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在检查依赖...
python -m muyan --check
echo.
echo 启动 幕言...
python -m muyan
if errorlevel 1 pause
