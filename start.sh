#!/usr/bin/env bash
# office-toolbox 启动脚本（macOS / Linux）
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3，请先安装 Python 3.10 及以上版本"
    exit 1
fi

if [ ! -d .venv ]; then
    echo "[1/3] 首次运行，正在创建虚拟环境..."
    python3 -m venv .venv
fi

echo "[2/3] 检查壳依赖..."
.venv/bin/pip install -q -r requirements.txt

echo "[3/3] 启动工具箱，浏览器将自动打开..."
.venv/bin/python -m src.main
