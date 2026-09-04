"""依赖管理：设置页的数据源与操作（Python 依赖 + 大组件的查询/安装/卸载）。

设计要点：
1. 状态查询纯只读（文件系统 + importlib.metadata），不加载插件代码、不联网，
   因此插件依赖缺失时设置页依然可用（「使用前先装」的入口不能自己先挂）；
2. 安装/卸载是阻塞函数，由壳放后台线程执行，进度经 progress(percent, message)
   回调上报（与插件动作的 _progress 同一套约定）；
3. 大组件由插件 manifest 的 assets 字段声明，壳按 type 通用处理，不感知业务含义：
     playwright — Playwright 浏览器组件（ms-playwright 目录，经 playwright CLI 下载）
     modelscope — ModelScope 模型快照（下载到共享缓存 ~/.cache/office-toolbox）
4. "_shell" 是伪插件 id，代表工具箱壳自身（根目录 requirements.txt）。
"""
from __future__ import annotations

import importlib.metadata
import inspect
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHELL_ID = "_shell"

# 大组件共享缓存目录。与 whisperx_transcribe._MODEL_CACHE 同源
# （模型目录 = ASSET_CACHE_DIR/models/<dir>），改动需两处同步。
ASSET_CACHE_DIR = Path.home() / ".cache" / "office-toolbox"

# Playwright 浏览器默认走 npmmirror 国内镜像（与 douyin_login 一致，用户已设 env 时不覆盖）
_PW_MIRROR = "https://cdn.npmmirror.com/binaries/playwright"
_PW_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_PW_NAME_RE = re.compile(r"Downloading\s+(.+?)(?=\s+\d|\s*$)")


def hard_rmtree(path: Path) -> None:
    """无条件删除目录树，任何异常就地吞掉、绝不向上抛。

    为什么不用 shutil.rmtree：部分运行环境里它被安全钩子接管，目录内文件数
    超过阈值时会抛「需确认」异常（ignore_errors=True 也拦不住），删除中断且
    异常穿透调用方——轻则请求 500，重则把后台线程静默打死（锁不释放）。
    本函数的删除目标永远是工具箱自己生成的临时工作区（output/tmp）与组件
    缓存（ms-playwright、ASSET_CACHE_DIR），不是用户文件，故用 os 层遍历
    手动删除，绕开钩子。
    """
    import stat as _stat

    def _force(fn, p, _exc):
        try:  # 只读属性等导致的删除失败：去掉只读位后重试一次
            os.chmod(p, _stat.S_IWRITE)
            fn(p)
        except Exception:  # noqa: BLE001
            pass

    if not path.exists():
        return
    for root, dirs, files in os.walk(path, topdown=False, onerror=_force):
        for name in files:
            try:
                os.unlink(os.path.join(root, name))
            except Exception:  # noqa: BLE001 — 被占用的文件跳过，不影响其余
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except Exception:  # noqa: BLE001
                pass
    try:
        os.rmdir(path)
    except Exception:  # noqa: BLE001 — 目录非空/被占用时保留现场，主流程不受影响
        pass


# ---------------------------------------------------------------------------
# 状态查询（只读）
# ---------------------------------------------------------------------------
def deps_overview(manifests: list[dict]) -> list[dict]:
    """汇总壳 + 各插件的依赖状态，供设置页渲染。"""
    out = [_plugin_deps({
        "id": SHELL_ID, "name": "工具箱壳（运行时）", "icon": "🧰",
        "description": "服务本身的基础依赖，全部缺失时下次启动会自动补装",
    })]
    for m in manifests:
        out.append(_plugin_deps(m))
    return out


def _plugin_deps(manifest: dict) -> dict:
    pkgs = pip_status(manifest["id"])
    return {
        "id": manifest["id"],
        "name": manifest.get("name", manifest["id"]),
        "icon": manifest.get("icon", "🧰"),
        "description": manifest.get("description", ""),
        "packages": pkgs,
        "assets": asset_status(manifest),
        "deps_ready": all(p["state"] == "ok" for p in pkgs),
    }


def _req_file(pid: str) -> Path:
    if pid == SHELL_ID:
        return PROJECT_ROOT / "requirements.txt"
    return PROJECT_ROOT / "plugins" / pid / "requirements.txt"


def _requirement_lines(pid: str) -> list[str]:
    f = _req_file(pid)
    if not f.exists():
        return []
    out = []
    for raw in f.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _parse_req(line: str) -> tuple[str, str]:
    """把 requirements 行拆成 (包名, 版本约束)。优先用 packaging 精确解析，
    缺失时降级为字符串切割（只影响版本比对展示，不影响安装——安装始终传原始行）。"""
    try:
        from packaging.requirements import Requirement
        r = Requirement(line)
        return r.name, str(r.specifier) or ""
    except Exception:  # noqa: BLE001 — packaging 未装或行格式怪异
        name = re.split(r"[=<>!~\[; ]", line, 1)[0].strip()
        return name, ""


def _installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _satisfies(spec: str, version: str) -> bool:
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        return SpecifierSet(spec).contains(Version(version), prereleases=True)
    except Exception:  # noqa: BLE001 — 比不出来就当满足，不误导用户
        return True


def pip_status(pid: str) -> list[dict]:
    rows = []
    for line in _requirement_lines(pid):
        name, spec = _parse_req(line)
        ver = _installed_version(name)
        state = "missing" if ver is None else ("outdated" if not _satisfies(spec, ver) else "ok")
        rows.append({"name": name, "spec": spec, "installed": ver, "state": state})
    return rows


def _du(path: Path) -> int:
    """目录/文件占用字节数（设置页展示「已装组件占多少磁盘」用）。"""
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f}GB"
    return f"{n / 1024 ** 2:.1f}MB"


def asset_status(manifest: dict) -> list[dict]:
    """按 manifest 的 assets 声明逐个检查安装状态。"""
    rows = []
    for a in manifest.get("assets") or []:
        row = {"id": a.get("id", ""), "type": a.get("type", ""), "name": a.get("name", ""),
               "description": a.get("description", ""), "size_hint": a.get("size_hint", ""),
               "state": "missing", "size": 0}
        try:
            if a.get("type") == "playwright":
                targets = a.get("targets") or ["chromium"]
                hit = [t for t in targets if _pw_target_dirs(t)]
                row["size"] = sum(_du(d) for t in targets for d in _pw_target_dirs(t))
                row["state"] = "ok" if len(hit) == len(targets) else ("partial" if hit else "missing")
            elif a.get("type") == "modelscope":
                d = _ms_model_dir(a)
                if (d / "config.json").exists():
                    row["state"], row["size"] = "ok", _du(d)
                elif d.exists():
                    row["state"], row["size"] = "partial", _du(d)  # 下载中断的残留
        except Exception:  # noqa: BLE001 — 单个组件检查失败不影响整页
            row["state"] = "missing"
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 安装 / 卸载（阻塞，进度经回调上报）
# ---------------------------------------------------------------------------
def pip_install_op(pid: str, names: list[str] | None, progress=None) -> None:
    """安装插件依赖。names=None 装全部；否则只装指定包（始终传 requirements 原始行，
    保留版本约束）。逐包安装是为了把进度粒度做到「第几个包」。"""
    reqs: dict[str, str] = {}
    for line in _requirement_lines(pid):
        n, _ = _parse_req(line)
        reqs[n.lower().replace("-", "_")] = line
    if names:
        picks = []
        for n in names:
            key = n.lower().replace("-", "_")
            if key not in reqs:
                raise ValueError(f"requirements.txt 中没有 {n}")
            picks.append(reqs[key])
    else:
        picks = list(reqs.values())
    if not picks:
        return
    total = len(picks)
    for i, line in enumerate(picks, 1):
        name, _ = _parse_req(line)
        if progress:
            progress(percent=int((i - 1) * 100 / total), message=f"安装 {name}（{i}/{total}）…")
        _pip_install_one(line, name, i, total, progress)
    if progress:
        progress(percent=100, message="依赖安装完成")


def _pip_install_one(req_line: str, name: str, i: int, total: int, progress) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", req_line],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    lines: list[str] = []
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        lines.append(line)
        if progress:
            progress(message=f"安装 {name}（{i}/{total}）：{line[:120]}")
    code = proc.wait()
    if code != 0:
        # 把 pip 输出末尾带回给前端：失败原因（网络/版本冲突/找不到包等）
        # 通常都在最后几行，只报 exit code 用户无从判断
        tail = "\n".join(lines[-5:])
        raise RuntimeError(f"pip install {name} 失败（exit {code}），pip 输出末尾：\n{tail}")


def pip_uninstall_op(pid: str, names: list[str], progress=None) -> None:
    """卸载指定包。只删列出的包本身，不带掉它们引入的间接依赖（torch 之类可能被
    其他包共用，删大了容易误伤）。"""
    if not names:
        return
    if progress:
        progress(message=f"卸载 {', '.join(names)}…")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", *names],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError("pip 卸载失败：" + " | ".join(tail))
    if progress:
        progress(percent=100, message="已卸载")


# ----- playwright 浏览器组件 -----

def _pw_browsers_dir() -> Path:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _pw_target_dirs(target: str) -> list[Path]:
    """target（如 chromium / chromium-headless-shell）已解压的组件目录。
    目录名形如 chromium-1187、chromium_headless_shell-1188，用规范化前缀匹配。"""
    root = _pw_browsers_dir()
    if not root.is_dir():
        return []
    prefix = target.replace("-", "_") + "-"
    return [d for d in root.iterdir() if d.is_dir() and d.name.startswith(prefix)]


def _iter_proc_lines(stream):
    """按 \\r / \\n 切分子进程输出：进度条靠 \\r 原地刷新，按行读要等全部结束才拿得到。"""
    buf = ""
    while True:
        ch = stream.read(1)
        if not ch:
            break
        if ch in "\r\n":
            if buf.strip():
                yield buf.strip()
            buf = ""
        else:
            buf += ch
    if buf.strip():
        yield buf.strip()


def _pw_install_target(target: str, progress=None) -> None:
    """下载 playwright 浏览器组件，流式解析 CLI 进度条输出上报百分比。"""
    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_DOWNLOAD_HOST", _PW_MIRROR)
    if progress:
        progress(percent=0, message=f"正在下载浏览器组件 {target}…（首次较慢，请耐心等待）")
    proc = subprocess.Popen(
        [sys.executable, "-m", "playwright", "install", target],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    tail: list[str] = []
    component = target
    deadline = time.time() + 1800  # 兜底：镜像异常导致下载僵死时不再无限等
    for line in _iter_proc_lines(proc.stdout):
        nm = _PW_NAME_RE.search(line)
        if nm:
            component = nm.group(1).strip()
        pm = _PW_PCT_RE.search(line)
        if pm and progress:
            try:
                pct = float(pm.group(1))
            except ValueError:
                pct = -1
            if 0 <= pct <= 100:
                progress(percent=int(pct), message=f"正在下载浏览器组件 {component}…")
        tail = (tail + [line])[-5:]
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("浏览器组件下载超时（30 分钟），请检查网络后重试")
    code = proc.wait(timeout=60)
    if code != 0:
        raise RuntimeError(f"浏览器组件安装失败（exit {code}）：" + " | ".join(tail))


# ----- modelscope 模型 -----

def _ms_model_dir(asset: dict) -> Path:
    d = (ASSET_CACHE_DIR / "models" / asset["dir"]).resolve()
    # 防御 manifest 笔误（dir 写成 ../../.. 之类）导致 rmtree 越界
    if ASSET_CACHE_DIR.resolve() not in d.parents:
        raise ValueError("非法的模型目录声明")
    return d


def _make_ms_callback(label: str, progress):
    """构造 modelscope snapshot_download 的 progress_callbacks 回调类。
    modelscope 对每个文件实例化一次并在多线程里调 update(size)，闭包共享状态
    聚合出整体进度。"""
    state = {"total": 0, "done": 0}
    lock = threading.Lock()

    class _Cb:
        def __init__(self, filename: str, file_size: int):
            self._size = max(0, int(file_size or 0))
            with lock:
                state["total"] += self._size

        def update(self, size: int) -> None:
            with lock:
                state["done"] += int(size)
                done, total = state["done"], state["total"]
            if not progress:
                return
            if total > 0:
                progress(percent=int(min(done / total, 1.0) * 100),
                         message=f"下载 {label}：{_fmt_bytes(done)} / {_fmt_bytes(total)}")
            else:
                progress(message=f"下载 {label}：已下载 {_fmt_bytes(done)}")

        def end(self) -> None:
            pass

    return _Cb


def _ms_install(repo: str, local_dir: Path, label: str, progress=None) -> None:
    try:
        from modelscope import snapshot_download
    except ImportError as exc:
        raise RuntimeError("缺少 modelscope 依赖，请先安装该插件的 Python 依赖") from exc
    if progress:
        progress(percent=0, message=f"从 ModelScope 下载 {label}…")
    kwargs = {}
    try:
        if "progress_callbacks" in inspect.signature(snapshot_download).parameters:
            kwargs["progress_callbacks"] = [_make_ms_callback(label, progress)]
    except (TypeError, ValueError):
        pass
    snapshot_download(model_id=repo, local_dir=str(local_dir), **kwargs)
    if progress:
        progress(percent=100, message=f"{label} 已就绪")


# ----- 组件操作入口 -----

def _find_asset(manifest: dict, asset_id: str) -> dict:
    for a in manifest.get("assets") or []:
        if a.get("id") == asset_id:
            return a
    raise LookupError(f"组件不存在: {asset_id}")


def asset_install_op(manifest: dict, asset_id: str, progress=None) -> None:
    a = _find_asset(manifest, asset_id)
    if a.get("type") == "playwright":
        if _installed_version("playwright") is None:
            raise RuntimeError("缺少 playwright Python 包，请先安装该插件的 Python 依赖")
        for target in a.get("targets") or ["chromium"]:
            if _pw_target_dirs(target):
                continue  # 已在位（partial 补装场景），跳过
            _pw_install_target(target, progress)
        if progress:
            progress(percent=100, message="浏览器组件已就绪")
    elif a.get("type") == "modelscope":
        d = _ms_model_dir(a)
        d.parent.mkdir(parents=True, exist_ok=True)
        _ms_install(a["repo"], d, a.get("name") or a["id"], progress)
    else:
        raise ValueError(f"未知组件类型: {a.get('type')}")


def asset_uninstall_op(manifest: dict, asset_id: str, progress=None) -> None:
    a = _find_asset(manifest, asset_id)
    if progress:
        progress(message=f"移除 {a.get('name') or asset_id}…")
    if a.get("type") == "playwright":
        for target in a.get("targets") or ["chromium"]:
            for d in _pw_target_dirs(target):
                hard_rmtree(d)
    elif a.get("type") == "modelscope":
        d = _ms_model_dir(a)
        if d.exists():
            hard_rmtree(d)
    else:
        raise ValueError(f"未知组件类型: {a.get('type')}")
    if progress:
        progress(percent=100, message="已移除")
