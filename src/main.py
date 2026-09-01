"""office-toolbox 服务入口：启动本地 Web 服务并自动打开浏览器。

用法：
    python -m src.main
"""
import json
import shutil
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .registry import DOWNLOADS_DIR, TMP_DIR, PluginRegistry

HOST = "127.0.0.1"
PORT = 8765

app = FastAPI(title="office-toolbox", docs_url=None, redoc_url=None)
registry = PluginRegistry()

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_workdir(workdir: Path):
    """任务结束后清理临时工作目录（壳的设计目标是文件暂存与自动清理）。

    执行结果已复制到下载区（DOWNLOADS_DIR），这里删除的是中间产物，
    不会动用户真正拿到的文件。忽略删除失败（如文件被占用），不影响主流程。
    """
    try:
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _cleanup_stale_tmp():
    """启动时清扫上次运行残留的临时任务目录（历史版本未实现清理，会无限累积）。"""
    try:
        for child in TMP_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# 下载产物保留时长：超过此时间仍未被下载的文件，由后台清扫线程自动删除
DOWNLOAD_TTL_SECONDS = 30 * 60


def _safe_delete(path: Path):
    """安全删除单个文件，忽略不存在 / 被占用等异常。"""
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _cleanup_expired_downloads():
    """兜底清理：删除下载区中超过 TTL 仍未被下载的孤儿文件。

    正常流程下，文件在用户点击下载后即删（见 download 路由的 BackgroundTasks），
    此函数只处理「用户拿到链接但始终没点」的情况，防止 downloads 无限累积。
    """
    try:
        now = time.time()
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > DOWNLOAD_TTL_SECONDS:
                _safe_delete(f)
    except Exception:  # noqa: BLE001
        pass


def _start_download_sweeper():
    """后台守护线程：每 5 分钟扫描一次下载区，清理过期文件。"""
    def _loop():
        while True:
            time.sleep(5 * 60)
            _cleanup_expired_downloads()
    threading.Thread(target=_loop, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    """首页：卡片墙（由前端根据 /api/plugins 动态渲染）。"""
    from .registry import PROJECT_ROOT

    html = PROJECT_ROOT / "src" / "web" / "index.html"
    return HTMLResponse(html.read_text(encoding="utf-8"))


@app.get("/api/plugins")
def list_plugins():
    """插件清单（manifest 摘要）。"""
    return {"plugins": registry.list_plugins()}


@app.post("/api/plugins/{pid}/run/{aid}")
async def run_action(pid: str, aid: str, files: list[UploadFile] | None = File(None),
                     params: str = Form("{}")):
    """执行插件动作：按需加载插件代码 -> 调用对应函数 -> 返回下载链接。"""
    manifest, action = registry.get_action(pid, aid)
    if not manifest or not action:
        return JSONResponse({"success": False, "error": f"插件/动作不存在: {pid}/{aid}"}, status_code=404)

    try:
        module = registry.load_module(pid)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)

    fn = getattr(module, "ACTIONS", {}).get(aid)
    if not fn:
        return JSONResponse({"success": False, "error": f"插件 {pid} 未实现动作 {aid}"}, status_code=500)

    # 工作目录：一次任务一个临时目录
    task_id = uuid.uuid4().hex
    workdir = TMP_DIR / task_id
    workdir.mkdir(parents=True)

    # 保存上传文件
    saved: dict[str, list[str]] = {}
    for upload in files or []:
        target = workdir / (upload.filename or f"file_{len(saved)}")
        target.write_bytes(await upload.read())
        saved.setdefault("files", []).append(str(target))

    # 解析参数（前端传 JSON 字符串）
    try:
        param_dict = json.loads(params or "{}")
    except json.JSONDecodeError:
        param_dict = {}

    # 调用插件（同步函数；转换类任务通常为 CPU/IO 密集，MVP 直接执行）
    try:
        outputs = fn(saved, param_dict, workdir)
    except Exception as exc:  # noqa: BLE001
        _cleanup_workdir(workdir)
        return JSONResponse({"success": False, "error": f"执行失败: {exc}"}, status_code=500)

    # 输出文件复制到下载区，返回下载链接
    # 插件可返回 Path 列表，或 dict 列表（{"path": ..., 其他字段透传给前端}，
    # 如音频切分返回每段的 start/end 供前端展示片段信息）
    results = []
    for item in outputs or []:
        if isinstance(item, dict):
            path_str = item.get("path", "")
            meta = {k: v for k, v in item.items() if k != "path"}
        else:
            path_str = item
            meta = {}
        if not path_str:
            continue
        path = Path(path_str)
        if not path.exists():
            continue
        target = DOWNLOADS_DIR / f"{task_id}_{path.name}"
        shutil.copy2(path, target)
        result = {"name": path.name, "url": f"/downloads/{target.name}"}
        result.update(meta)
        results.append(result)

    # 结果已复制进下载区，清理本次任务的临时工作目录
    _cleanup_workdir(workdir)

    if not results:
        return JSONResponse({"success": False, "error": "未生成任何输出文件"}, status_code=500)
    return {"success": True, "files": results}


@app.get("/downloads/{name}")
def download(name: str, background_tasks: BackgroundTasks):
    """下载生成的文件；浏览器取走后即删除本地副本（下载即删）。"""
    path = DOWNLOADS_DIR / name
    if not path.is_file():
        return JSONResponse({"success": False, "error": "文件不存在或已过期（可能已被自动清理）"}, status_code=404)
    # 响应发送完成后删除本地文件，实现"用完即删"
    background_tasks.add_task(_safe_delete, path)
    # 磁盘文件名带任务ID前缀（防多任务同名冲突），下载时去掉，还原干净的原文件名
    display_name = name.split("_", 1)[1] if "_" in name else name
    return FileResponse(path, filename=display_name)


@app.delete("/downloads/{name}")
def delete_download(name: str):
    """删除下载区文件（勾选下载完成后，清理本次任务剩余未取走的文件）。"""
    if "/" in name or "\\" in name or name in (".", ".."):
        return JSONResponse({"success": False, "error": "非法文件名"}, status_code=400)
    path = DOWNLOADS_DIR / name
    if path.is_file():
        _safe_delete(path)
        return {"success": True}
    return JSONResponse({"success": False, "error": "文件不存在或已过期"}, status_code=404)


def _open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://{HOST}:{PORT}")


def main():
    _cleanup_stale_tmp()
    _cleanup_expired_downloads()  # 启动即清掉上次遗留的未下载文件
    _start_download_sweeper()
    print(f"office-toolbox 已启动: http://{HOST}:{PORT}", flush=True)
    threading.Thread(target=_open_browser, daemon=True).start()
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    except OSError as exc:
        print(f"[错误] 启动失败：{exc}", flush=True)
        print("提示：请检查 8765 端口是否被其他程序占用。", flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
