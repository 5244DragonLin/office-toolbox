"""抖音扫码登录：Playwright 无头浏览器打开 SSO 登录页 → 提取二维码 → 轮询登录态。

为什么必须用真浏览器：抖音 SSO（sso.douyin.com）有字节安全 SDK 的浏览器环境校验，
纯 HTTP 请求（含 TLS 指纹伪装/手动补 cookie）一律被打回登录页 HTML，无法调通二维码
接口；就连 /passport/sso/login_page 路由都会 403。唯独浏览器直接访问
`sso.douyin.com/?aid=6383&service=...` 时登录 SPA 正常渲染并展示二维码（上游参考
项目 douyin-downloader 的登录同样基于 Playwright）。

页面上的二维码是前端用 get_qrcode 返回内容渲染的 base64 data-URI <img>，
直接把 src 返回给 Web 弹窗展示即可；扫码状态通过监听页面自身的
check_qrconnect 轮询响应获取，登录成功后浏览器里出现 sessionid cookie。

凭证去向：把浏览器里的全部 douyin cookie 拼成「k=v; k2=v2」字符串写入插件目录
cookies.txt（与手动粘贴共用同一机制），下载功能自动复用，无需任何改动。

线程模型：壳的每个请求可能在不同线程池线程里执行，而 Playwright 同步 API 要求
所有操作在同一线程——这里用一条专职浏览器线程串行处理所有浏览器操作。

依赖（可选，按需安装，浏览器组件自动下载）：
    pip install playwright
    （首次扫码时，缺失的 Chromium 浏览器组件会自动下载（约 100~200MB），
     默认使用 npmmirror 国内镜像加速下载，如遇网络问题可手动换源：
      set PLAYWRIGHT_DOWNLOAD_HOST=https://playwright.azureedge.net）

    ⚠️ Playwright 1.51+ 无头模式默认使用 chromium_headless_shell，
    浏览器组件安装命令会自动处理此差异，无需手动干预
"""
import base64
import os
import queue
import threading
import time
from pathlib import Path

# 默认使用 npmmirror 国内镜像下载 Chromium 浏览器（约 100~200MB）
os.environ.setdefault("PLAYWRIGHT_DOWNLOAD_HOST",
                      "https://cdn.npmmirror.com/binaries/playwright")

from douyin_fetch import COOKIES_FILE, _save_cookie, _invalidate_cookie

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
SSO_PAGE_URL = "https://sso.douyin.com/?aid=6383&service=https%3A%2F%2Fwww.douyin.com"
# 二维码有效期（抖音约 3 分钟），超时提示重新生成
QR_MAX_AGE_SECONDS = 180
# 临近过期阈值：生成二维码后超过这个时长、且仍无人扫码时，后端主动返回 refresh
# 要求前端重新生成，避免被动等到 180s 真正过期那一刻才换码。已扫码（qr_status
# == QR_SCANNED）分支已优先返回，不会打断用户正在手机上点确认的操作。
QR_REFRESH_THRESHOLD_SECONDS = 150

# check_qrconnect 返回的 data.code 状态码
QR_SCANNED = 1      # 已扫码，等待 App 上确认
QR_EXPIRED = 3      # 二维码已过期

# 两次接口之间共享的浏览器会话（个人工具箱，单会话即可）；
# 只允许在 _browser_thread 内访问
_SESSION: dict = {}

# 专职浏览器线程：壳的线程池每次请求可能换线程，Playwright 同步 API 不允许跨线程，
# 所有浏览器操作打包成闭包投递到这条线程串行执行
_task_q: "queue.Queue" = queue.Queue()
_browser_thread: threading.Thread | None = None


def _browser_worker_loop():
    while True:
        fn, reply = _task_q.get()
        try:
            reply.put((True, fn()))
        except BaseException as exc:  # noqa: BLE001 — 原样把异常送回调用方
            reply.put((False, exc))


def _run_in_browser_thread(fn, timeout: float = 120.0):
    global _browser_thread
    if _browser_thread is None or not _browser_thread.is_alive():
        _browser_thread = threading.Thread(
            target=_browser_worker_loop, daemon=True, name="douyin-login-browser")
        _browser_thread.start()
    reply: "queue.Queue" = queue.Queue()
    _task_q.put((fn, reply))
    ok, result = reply.get(timeout=timeout)
    if not ok:
        raise result
    return result


def _close_session():
    """关闭并清理扫码浏览器会话（须在浏览器线程内执行）。"""
    s = _SESSION
    pw = s.pop("pw", None)
    for key in ("page", "context", "browser"):
        obj = s.pop(key, None)
        try:
            if obj is not None:
                obj.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:  # noqa: BLE001
        pass


def _open_session() -> bytes:
    """启动无头浏览器打开 SSO 登录页，提取二维码 PNG（须在浏览器线程内执行）。"""
    from playwright.sync_api import sync_playwright

    _close_session()
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA,
                                      viewport={"width": 500, "height": 720},
                                      locale="zh-CN")
        page = context.new_page()

        state = {"qr_status": None}

        def on_response(resp):
            """监听登录页自身的轮询，拿扫码状态（1 已扫码 / 3 过期）。"""
            try:
                if "check_qrconnect" in resp.url:
                    data = (resp.json().get("data") or {})
                    state["qr_status"] = data.get("status")
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)
        page.goto(SSO_PAGE_URL, wait_until="domcontentloaded", timeout=40000)
        # 登录 SPA 渲染后，二维码以 base64 data-URI 形式显示在 <img> 里
        img = page.wait_for_selector("img[src^='data:image']", timeout=25000)
        src = img.get_attribute("src") or ""
        if "," not in src:
            raise RuntimeError("登录页已打开，但未能提取二维码，请重试一次")
        png = base64.b64decode(src.split(",", 1)[1])

        _SESSION.update({"pw": pw, "page": page, "context": context,
                         "browser": browser, "state": state,
                         "created_at": time.time()})
        return png
    except Exception:
        _close_session()
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        raise


def _setup_hint() -> str:
    return (
        "扫码登录需要安装 playwright Python 包，\n"
        "请执行：.venv\\Scripts\\python.exe -m pip install playwright\n"
        "安装后重新扫码，浏览器组件会自动下载。"
    )


def _auto_install_browser(progress=None):
    """自动下载 Playwright Chromium 浏览器组件（约 100~300MB，仅首次）。

    `playwright install` 的 CLI 用 \\r 原地刷新下载进度条，这里流式解析 stdout
    中的百分比并经 progress 回调上报，让前端登录弹窗能实时展示下载进度。
    """
    import re
    import subprocess
    import sys as _sys

    _pct_re = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
    _name_re = re.compile(r"Downloading\s+(.+?)(?=\s+\d|\s*$)")

    def _iter_lines(stream):
        """按 \\r / \\n 切分子进程输出：进度条靠 \\r 原地刷新，按行读只能等
        全部下载结束才拿到内容，无法做实时进度。"""
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

    def _install(target: str):
        print(f"[douyin_login] 正在下载 Playwright 浏览器组件 {target}（约 100~300MB，仅首次）…",
              flush=True)
        if progress:
            progress(percent=0, message=f"首次扫码需下载浏览器组件（约 100~300MB）：{target}…")
        proc = subprocess.Popen(
            [_sys.executable, "-m", "playwright", "install", target],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        tail: list[str] = []
        component = target
        deadline = time.time() + 900  # 兜底：镜像异常导致下载僵死时不再无限等
        for line in _iter_lines(proc.stdout):
            nm = _name_re.search(line)
            if nm:
                component = nm.group(1).strip()
            pm = _pct_re.search(line)
            if pm:
                try:
                    pct = float(pm.group(1))
                except ValueError:
                    pct = -1
                if 0 <= pct <= 100 and progress:
                    progress(percent=int(pct), message=f"正在下载浏览器组件 {component}…")
            tail = (tail + [line])[-5:]
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError("浏览器组件下载超时（15 分钟），请检查网络后重试")
        code = proc.wait(timeout=60)
        if code != 0:
            raise subprocess.CalledProcessError(code, proc.args, output="\n".join(tail))

    try:
        _install("chromium")
    except subprocess.CalledProcessError:
        # 新版 Playwright 可能用 headless shell，补充安装
        _install("chromium-headless-shell")


def login_start(progress=None) -> dict:
    """生成登录二维码，返回 {"image": dataURL} 供前端弹窗直接展示。

    progress：壳注入的进度回调（percent/message）。首次扫码缺失浏览器组件时，
    会触发约 100~300MB 的组件下载，下载进度经此回调实时上报给登录弹窗。
    """
    try:
        png = _run_in_browser_thread(lambda: _open_session())
    except ImportError as exc:
        raise RuntimeError(f"未安装 playwright。\n{_setup_hint()}") from exc
    except Exception as exc:
        msg = str(exc)
        if "Executable doesn't exist" in msg or "cannot find browser" in msg.lower():
            _auto_install_browser(progress)
            if progress:
                progress(percent=100, message="浏览器组件就绪，正在打开登录页…")
            # 装完后重试一次
            png = _run_in_browser_thread(lambda: _open_session())
        else:
            raise RuntimeError(f"启动浏览器失败：{exc}") from exc
    image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return {"image": image}


def login_poll() -> dict:
    """查询一次扫码状态，返回 {"status": waiting/scanned/confirmed/expired/error, "message": str}。

    confirmed 时凭证已写入 cookies.txt，所有下载功能自动生效。
    """
    if not _SESSION.get("context"):
        return {"status": "error", "message": "请先重新打开扫码登录生成二维码"}
    if time.time() - _SESSION.get("created_at", 0) > QR_MAX_AGE_SECONDS:
        _run_in_browser_thread(_close_session)
        return {"status": "expired", "message": "二维码已过期"}

    try:
        cookies = _run_in_browser_thread(
            lambda: _SESSION["context"].cookies(["https://www.douyin.com"]), timeout=30)
    except Exception:  # noqa: BLE001 — 浏览器已崩溃/被关闭
        return {"status": "error", "message": "登录会话已失效，请重新生成二维码"}

    if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("value"))
        _save_cookie(cookie_str)
        _run_in_browser_thread(_close_session)
        return {"status": "confirmed",
                "message": "登录成功，抖音凭证已保存，所有下载功能已自动生效"}

    status = (_SESSION.get("state") or {}).get("qr_status")
    if status == QR_EXPIRED:
        _run_in_browser_thread(_close_session)
        return {"status": "expired", "message": "二维码已失效"}
    if status == QR_SCANNED:
        # 已扫码：绝不主动刷新，保护用户正在手机上进行的确认操作
        return {"status": "scanned", "message": "已扫码，请在手机上确认"}
    # 到这里一定是 waiting（未扫码）。临近过期则主动要求前端刷新；前端重新调
    # start 会开一个带新二维码的浏览器会话，旧会话在此清理释放资源。
    if (time.time() - _SESSION.get("created_at", 0)) > QR_REFRESH_THRESHOLD_SECONDS:
        _run_in_browser_thread(_close_session)
        return {"status": "refresh", "message": "二维码即将过期，自动刷新中…"}
    return {"status": "waiting", "message": "等待扫码…"}


def login_logout(output_dir: Path) -> str:
    """清除已保存的抖音登录凭证（cookies.txt）。"""
    existed = COOKIES_FILE.exists()
    _invalidate_cookie()
    f = output_dir / ("已退出登录.txt" if existed else "当前未登录.txt")
    f.write_text(
        "已清除保存的抖音登录凭证（cookies.txt），之后以游客身份下载。\n" if existed
        else "本机没有保存过抖音登录凭证，无需退出。\n",
        encoding="utf-8-sig",
    )
    return str(f)
