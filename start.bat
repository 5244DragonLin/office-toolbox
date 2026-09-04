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

set "NEED_CREATE=0"
if exist ".venv\Scripts\python.exe" (
    rem 文件存在不代表可用：从其他电脑拷贝来的 .venv 指向不存在的解释器，运行才会暴露
    ".venv\Scripts\python.exe" -c "pass" >nul 2>nul
    if errorlevel 1 (
        echo [1/3] 检测到 .venv 虚拟环境损坏，可能来自其他电脑或已卸载的 Python，正在自动重建...
        rmdir /s /q .venv
        set "NEED_CREATE=1"
    ) else (
        echo [1/3] 虚拟环境已就绪
    )
) else (
    set "NEED_CREATE=1"
)

if "%NEED_CREATE%"=="1" (
    echo [1/3] 正在创建虚拟环境...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败，请检查 Python 安装
        pause
        exit /b 1
    )
)

echo [2/3] 检查壳依赖...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [错误] 壳依赖安装失败，请检查网络后重试；也可手动运行下面命令查看详细报错：
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo [3/3] 启动工具箱，浏览器将自动打开...
echo 提示: 关闭此窗口或按 Ctrl+C 即可停止服务
echo.
".venv\Scripts\python.exe" -u -m src.main
if errorlevel 1 (
    echo.
    echo [错误] 工具箱异常退出，具体原因请查看上方日志。若为 8765 端口被占用，关闭对应程序后重试即可
)

pause
