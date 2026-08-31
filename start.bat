@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   office-toolbox 个人办公工具箱
echo ============================================

set "PYCMD=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [错误] 未找到 Python，请先安装 Python 3.10 及以上版本
        echo 下载地址: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    set "PYCMD=py -3"
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 首次运行，正在创建虚拟环境...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败，请检查 Python 安装
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在，跳过创建
)

echo [2/3] 检查壳依赖...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt

echo [3/3] 启动工具箱，浏览器将自动打开...
echo 提示: 关闭此窗口或按 Ctrl+C 即可停止服务
echo.
".venv\Scripts\python.exe" -u -m src.main
if errorlevel 1 (
    echo.
    echo [错误] 服务启动失败，请检查 8765 端口是否被其他程序占用
)

pause
