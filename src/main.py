"""office-toolbox 服务入口：启动本地 Web 服务并自动打开浏览器。

用法：
    python -m src.main
"""
import inspect
import json
import re
import shutil
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import deps
from .registry import DOWNLOADS_DIR, TMP_DIR, PluginRegistry

HOST = "127.0.0.1"
PORT = 8765

app = FastAPI(title="office-toolbox", docs_url=None, redoc_url=None)
registry = PluginRegistry()

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

# 任务进度表：task_id -> {"percent": 0-100, "message": str}
# 插件通过 params["_progress"] 回调写入，前端轮询 /api/progress/{task_id} 读取
PROGRESS: dict[str, dict] = {}
_progress_lock = threading.Lock()


def _report_progress(task_id: str):
    """生成绑定到 task_id 的进度回调，注入插件 params。"""
    def _cb(percent=None, message=None):
        with _progress_lock:
            entry = PROGRESS.setdefault(task_id, {})
            if percent is not None:
                entry["percent"] = max(0, min(100, int(percent)))
            if message is not None:
                entry["message"] = str(message)[:200]
    return _cb


# 登录准备进度表：plugin_id -> {"percent": 0-100, "message": str, "active": bool}
# 生成二维码可能伴随大文件下载（如首次扫码时拉取 Playwright 浏览器组件），
# start 路由同步阻塞期间，前端轮询 /api/login/{pid}/progress 展示进度
LOGIN_PROGRESS: dict[str, dict] = {}
_login_progress_lock = threading.Lock()


def _report_login_progress(pid: str):
    """生成绑定到插件 id 的登录进度回调，注入 LOGIN_PROVIDER["start"]。"""
    def _cb(percent=None, message=None):
        with _login_progress_lock:
            entry = LOGIN_PROGRESS.setdefault(pid, {})
            if percent is not None:
                entry["percent"] = max(0, min(100, int(percent)))
            if message is not None:
                entry["message"] = str(message)[:200]
            entry["active"] = True
    return _cb


def _cleanup_workdir(workdir: Path):
    """任务结束后清理临时工作目录（壳的设计目标是文件暂存与自动清理）。

    执行结果已复制到下载区（DOWNLOADS_DIR），这里删除的是中间产物，
    不会动用户真正拿到的文件。用 deps.hard_rmtree（不走 shutil.rmtree：
    部分运行环境里它被安全钩子接管，文件数一多就抛确认异常，异常从
    finally 窜出会把整个请求打成 500），并再兜一层异常，清理失败绝不
    影响主流程。
    """
    try:
        deps.hard_rmtree(workdir)
    except Exception:  # noqa: BLE001
        pass


def _cleanup_stale_tmp():
    """启动时清扫上次运行残留的临时任务目录（历史版本未实现清理，会无限累积）。"""
    try:
        for child in TMP_DIR.iterdir():
            if child.is_dir():
                deps.hard_rmtree(child)
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
                     params: str = Form("{}"), task_id: str = Form("")):
    """执行插件动作：按需加载插件代码 -> 调用对应函数 -> 返回下载链接。

    task_id：前端生成的任务标识，用于轮询执行进度（/api/progress/{task_id}）。
    """
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

    # 工作目录：一次任务一个临时目录；任务标识不合法时退回服务端生成
    if not re.fullmatch(r"[0-9a-f]{8,32}", task_id or ""):
        task_id = uuid.uuid4().hex
    workdir = TMP_DIR / task_id
    workdir.mkdir(parents=True)

    try:
        # 保存上传文件
        saved: dict[str, list[str]] = {}
        for upload in files or []:
            target = workdir / (upload.filename or f"file_{len(saved)}")
            target.write_bytes(await upload.read())
            saved.setdefault("files", []).append(str(target))

        # 解析参数（前端传 JSON 字符串），并注入进度回调供插件上报进度
        try:
            param_dict = json.loads(params or "{}")
        except json.JSONDecodeError:
            param_dict = {}
        param_dict["_progress"] = _report_progress(task_id)

        # 调用插件（转换类任务通常为 CPU/IO 密集；批量下载可能持续很久，
        # 放入线程池执行，避免阻塞事件循环导致整个服务卡死）
        try:
            outputs = await run_in_threadpool(fn, saved, param_dict, workdir)
        except Exception as exc:  # noqa: BLE001
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
            # 文件名必须整体 URL 编码后再放进链接：抖音图文标题常以 # 话题开头，
            # 而 # 在 URL 里是「页内锚点」分隔符——不编码的话浏览器只会请求 # 之前
            # 的半截路径，服务端必然 404，浏览器把错误 JSON 存下来就变成「坏文件」。
            result = {"name": path.name, "url": f"/downloads/{quote(target.name)}"}
            result.update(meta)
            results.append(result)

        if not results:
            return JSONResponse({"success": False, "error": "未生成任何输出文件"}, status_code=500)
        return {"success": True, "files": results}
    finally:
        with _progress_lock:
            PROGRESS.pop(task_id, None)
        _cleanup_workdir(workdir)


@app.get("/api/progress/{task_id}")
def get_progress(task_id: str):
    """查询任务执行进度（插件未上报时 progress 为 null，前端据此决定是否展示进度条）。"""
    with _progress_lock:
        snapshot = dict(PROGRESS.get(task_id) or {})
    return {"success": True, "progress": snapshot or None}


# ---------------------------------------------------------------------------
# 扫码登录：通用契约（插件在模块级暴露 LOGIN_PROVIDER = {"start": fn, "poll": fn}
# 即可获得扫码登录能力；start 返回 {"image": dataURL}，poll 返回
# {"status": waiting/scanned/confirmed/expired/error, "message": str}，
# 前端弹窗展示二维码并轮询。与具体插件的登录实现完全解耦。
# ---------------------------------------------------------------------------

def _get_login_provider(pid: str) -> dict:
    module = registry.load_module(pid)
    provider = getattr(module, "LOGIN_PROVIDER", None)
    if not provider or not callable(provider.get("start")) or not callable(provider.get("poll")):
        raise LookupError(f"插件 {pid} 不支持扫码登录")
    return provider


@app.post("/api/login/{pid}/start")
def login_start_route(pid: str):
    """生成登录二维码（返回 base64 dataURL，前端弹窗直接展示）。

    首次扫码可能触发大文件下载（如 Playwright 浏览器组件），本路由同步阻塞到
    二维码就绪才返回；期间前端轮询 /api/login/{pid}/progress 展示下载进度。
    """
    try:
        provider = _get_login_provider(pid)
    except LookupError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=404)
    except Exception as exc:  # noqa: BLE001 — 含插件依赖缺失等
        return JSONResponse({"success": False, "message": str(exc)}, status_code=500)

    # 进度回调：provider["start"] 声明了 progress 参数（或 **kwargs）才注入，兼容旧契约
    cb = _report_login_progress(pid)
    with _login_progress_lock:
        LOGIN_PROGRESS[pid] = {"active": True, "message": "准备中…"}
    try:
        accepts = False
        try:
            sig = inspect.signature(provider["start"])
            accepts = ("progress" in sig.parameters
                       or any(p.kind is inspect.Parameter.VAR_KEYWORD
                              for p in sig.parameters.values()))
        except (TypeError, ValueError):
            pass
        result = provider["start"](progress=cb) if accepts else provider["start"]()
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"success": False, "message": f"生成二维码失败：{exc}"}, status_code=500)
    finally:
        with _login_progress_lock:
            LOGIN_PROGRESS.setdefault(pid, {})["active"] = False


@app.get("/api/login/{pid}/progress")
def login_progress_route(pid: str):
    """查询登录准备进度（如首次扫码下载浏览器组件），前端在 start 返回前轮询。"""
    with _login_progress_lock:
        snapshot = dict(LOGIN_PROGRESS.get(pid) or {})
    return {"success": True, "progress": snapshot or None}


@app.get("/api/login/{pid}/poll")
def login_poll_route(pid: str):
    """查询一次扫码状态（前端弹窗每 1~2 秒调用）。"""
    try:
        provider = _get_login_provider(pid)
    except LookupError as exc:
        return JSONResponse({"success": False, "status": "error", "message": str(exc)}, status_code=404)
    try:
        result = provider["poll"]()
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"success": False, "status": "error",
                             "message": f"查询扫码状态失败：{exc}"}, status_code=500)


# ---------------------------------------------------------------------------
# 设置页：依赖管理（Python 依赖 + 大组件的查询 / 安装 / 卸载）
# ---------------------------------------------------------------------------

class _PipBody(BaseModel):
    pid: str
    names: list[str] | None = None   # install：None=装全部；uninstall：必填


class _AssetBody(BaseModel):
    pid: str
    asset_id: str


_setup_op_lock = threading.Lock()   # 同一时间只允许一个安装/卸载操作
_setup_last_op: str | None = None   # 上一个操作 id（启动新操作时清掉旧进度）


def _start_setup_op(fn) -> str | None:
    """后台线程执行 fn(report)，进度写入 PROGRESS，前端轮询 /api/progress/{op_id}。

    与插件任务共用同一张进度表；区别在于任务结束由 run_action 清理，而设置操作
    保留终态（done/error）直到下一个操作覆盖，保证前端最后一次轮询能拿到结果。
    """
    global _setup_last_op
    if not _setup_op_lock.acquire(blocking=False):
        return None
    op_id = uuid.uuid4().hex[:16]
    with _progress_lock:
        if _setup_last_op:
            PROGRESS.pop(_setup_last_op, None)
        _setup_last_op = op_id

    def _run():
        try:
            report = _report_progress(op_id)
            with _progress_lock:
                PROGRESS.setdefault(op_id, {})["done"] = False
            fn(report)
            with _progress_lock:
                PROGRESS.setdefault(op_id, {}).update({"done": True, "percent": 100})
        except BaseException as exc:  # noqa: BLE001 — 连 SystemExit 也兜住：
            # 运行环境的安全钩子可能抛 SystemExit，若只捕 Exception，线程静默
            # 死亡且 _setup_op_lock 永不释放，后续所有安装/卸载都会 409 卡死
            with _progress_lock:
                PROGRESS.setdefault(op_id, {}).update({"done": True, "error": str(exc)})
        finally:
            _setup_op_lock.release()

    threading.Thread(target=_run, daemon=True, name="setup-op").start()
    return op_id


def _manifest_of(pid: str) -> dict | None:
    for m in registry.manifests():
        if m.get("id") == pid:
            return m
    return None


@app.get("/api/deps")
def deps_route():
    """设置页数据：壳 + 各插件的 Python 依赖与大组件安装状态。"""
    return {"success": True, "plugins": deps.deps_overview(registry.manifests())}


@app.post("/api/setup/pip-install")
def setup_pip_install(body: _PipBody):
    if body.pid != deps.SHELL_ID and not _manifest_of(body.pid):
        return JSONResponse({"success": False, "error": f"插件不存在: {body.pid}"}, status_code=404)
    op_id = _start_setup_op(lambda report: deps.pip_install_op(body.pid, body.names, report))
    if not op_id:
        return JSONResponse({"success": False, "error": "已有安装/卸载操作进行中，请等它完成"}, status_code=409)
    return {"success": True, "op_id": op_id}


@app.post("/api/setup/pip-uninstall")
def setup_pip_uninstall(body: _PipBody):
    if not body.names:
        return JSONResponse({"success": False, "error": "缺少要卸载的包名"}, status_code=400)
    if body.pid != deps.SHELL_ID and not _manifest_of(body.pid):
        return JSONResponse({"success": False, "error": f"插件不存在: {body.pid}"}, status_code=404)
    op_id = _start_setup_op(lambda report: deps.pip_uninstall_op(body.pid, body.names, report))
    if not op_id:
        return JSONResponse({"success": False, "error": "已有安装/卸载操作进行中，请等它完成"}, status_code=409)
    return {"success": True, "op_id": op_id}


@app.post("/api/setup/asset-install")
def setup_asset_install(body: _AssetBody):
    manifest = _manifest_of(body.pid)
    if not manifest:
        return JSONResponse({"success": False, "error": f"插件不存在: {body.pid}"}, status_code=404)
    op_id = _start_setup_op(lambda report: deps.asset_install_op(manifest, body.asset_id, report))
    if not op_id:
        return JSONResponse({"success": False, "error": "已有安装/卸载操作进行中，请等它完成"}, status_code=409)
    return {"success": True, "op_id": op_id}


@app.post("/api/setup/asset-uninstall")
def setup_asset_uninstall(body: _AssetBody):
    manifest = _manifest_of(body.pid)
    if not manifest:
        return JSONResponse({"success": False, "error": f"插件不存在: {body.pid}"}, status_code=404)
    op_id = _start_setup_op(lambda report: deps.asset_uninstall_op(manifest, body.asset_id, report))
    if not op_id:
        return JSONResponse({"success": False, "error": "已有安装/卸载操作进行中，请等它完成"}, status_code=409)
    return {"success": True, "op_id": op_id}


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
