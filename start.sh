#!/usr/bin/env bash
# office-toolbox 启动脚本（macOS / Linux）
# 行为保持与 start.bat（Windows 版）一致
set -u

cd "$(dirname "$0")" || exit 1

echo "============================================"
echo "  office-toolbox 个人办公工具箱"
echo "============================================"

# 探测可用的 Python 命令：优先 python3，其次 python（需确认是 3.10+）
PYCMD=""
if command -v python3 >/dev/null 2>&1; then
    PYCMD="python3"
elif command -v python >/dev/null 2>&1; then
    if python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
        PYCMD="python"
    fi
fi

if [ -z "$PYCMD" ]; then
    echo "[错误] 未找到 Python，请先安装 Python 3.10 及以上版本"
    echo "下载地址: https://www.python.org/downloads/"
    read -r -p "按回车键退出..." _
    exit 1
fi

# 虚拟环境：缺失或已损坏则重建
NEED_CREATE=0
if [ -x ".venv/bin/python" ]; then
    # 文件存在不代表可用：从其他电脑拷贝来的 .venv 指向不存在的解释器，运行才会暴露
    if ! .venv/bin/python -c "pass" >/dev/null 2>&1; then
        echo "[1/3] 检测到 .venv 虚拟环境损坏，可能来自其他电脑或已卸载的 Python，正在自动重建..."
        rm -rf .venv
        NEED_CREATE=1
    else
        echo "[1/3] 虚拟环境已就绪"
    fi
else
    NEED_CREATE=1
fi

if [ "$NEED_CREATE" -eq 1 ]; then
    echo "[1/3] 正在创建虚拟环境..."
    if ! $PYCMD -m venv .venv; then
        echo "[错误] 虚拟环境创建失败，请检查 Python 安装"
        read -r -p "按回车键退出..." _
        exit 1
    fi
fi

echo "[2/3] 检查壳依赖..."
if ! .venv/bin/python -m pip install -q -r requirements.txt; then
    echo "[错误] 壳依赖安装失败，请检查网络后重试；也可手动运行下面命令查看详细报错："
    echo "  .venv/bin/python -m pip install -r requirements.txt"
    read -r -p "按回车键退出..." _
    exit 1
fi

echo "[3/3] 启动工具箱，浏览器将自动打开..."
echo "提示: 关闭此窗口或按 Ctrl+C 即可停止服务"
echo
if ! .venv/bin/python -u -m src.main; then
    echo
    echo "[错误] 工具箱异常退出，具体原因请查看上方日志。若为 8765 端口被占用，关闭对应程序后重试即可"
    read -r -p "按回车键退出..." _
fi
