"""B站视频/音频/字幕下载核心逻辑，全部基于 yutto（不再依赖 yt-dlp）。

- 视频（单个 / 收藏夹 / 合集 / UP主空间）：yutto 批量/单视频下载
- 音频：yutto --audio-only 下载 m4a（aac 无损拷贝）；mp3 由 ffmpeg 转 192k
- 字幕：yutto --subtitle-only（注意：B站字幕接口需要登录，匿名请求拿不到字幕）
- 画质上限由 B站 按登录态决定：匿名最高 480P，提供 SESSDATA 后最高可达 1080P/4K（大会员）

ffmpeg 依赖说明（yutto 合并音视频 / mp3 转码都需要）：
- 系统已安装 ffmpeg 并加入 PATH → 优先使用系统版
- 系统未安装 ffmpeg → 自动从 imageio-ffmpeg 获取静态 ffmpeg 二进制
"""
import contextlib
import http.cookiejar
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# yutto 是核心下载引擎。这里显式 import 一方面确认依赖可用，
# 另一方面缺失时抛 ImportError，触发插件注册表按 requirements.txt 自动安装后重试。
import yutto  # noqa: F401

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")


def _get_ffmpeg_path() -> str:
    """查找可用的 ffmpeg 路径。

    返回值：
    - "ffmpeg"：系统 PATH 中已可用
    - 绝对路径：imageio-ffmpeg 提供的静态二进制
    - ""：两者均不可用
    """
    # 1. 系统 PATH
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return "ffmpeg"
    except Exception:
        pass

    # 2. imageio-ffmpeg 静态二进制
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        exe = get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return str(Path(exe).resolve())
    except Exception:
        pass

    return ""


def _ffmpeg_exe() -> list[str]:
    """返回可直接调用的 ffmpeg 命令前缀，找不到时抛错。"""
    path = _get_ffmpeg_path()
    if path == "ffmpeg":
        return ["ffmpeg"]
    if path:
        return [path]
    raise RuntimeError("未找到可用的 ffmpeg；请安装 ffmpeg 或执行 pip install imageio-ffmpeg")


def get_video_title(url: str) -> str:
    """通过 B站 Web API 获取视频标题（不下载，无需登录）。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m:
        query = f"bvid={m.group(1)}"
    else:
        m = re.search(r"av(\d+)", url, re.I)
        if not m:
            return "unknown"
        query = f"aid={m.group(1)}"
    req = urllib.request.Request(
        f"https://api.bilibili.com/x/web-interface/view?{query}",
        headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        return (data.get("data") or {}).get("title") or "unknown"
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取视频标题失败: %s", exc)
        return "unknown"


# ---------------------------------------------------------------------------
# yutto 下载（单视频 / 收藏夹 / 合集 / UP主空间 / 音频 / 字幕）
# ---------------------------------------------------------------------------

# yutto 清晰度等级：127:8K, 120:4K, 116:1080P60, 112:1080P+, 80:1080P, 74:720P60, 64:720P, 32:480P, 16:360P
# 实际清晰度受登录态限制：匿名最高 480P，登录后按账号权益解锁更高画质
YUTTO_QUALITY_MAP = {
    "best": "127",
    "4k": "120",
    "1080p": "80",
    "720p": "64",
    "480p": "32",
    "360p": "16",
}

# URL 类型识别（与 yutto extractor 支持的链接格式对应）
RE_FAVLIST = re.compile(r"space\.bilibili\.com/\d+/favlist", re.I)
RE_LIST = re.compile(r"(space\.bilibili\.com/\d+/lists/\d+|www\.bilibili\.com/list/\d+)", re.I)
RE_SPACE = re.compile(r"space\.bilibili\.com/\d+", re.I)
RE_SINGLE_VIDEO = re.compile(r"(www\.bilibili\.com/video/|b23\.tv/)", re.I)

# 中文字符（CJK 统一表意文字及其扩展区），用于「SRT 字幕只保留中文」过滤
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s))


def _filter_srt_chinese_only(path: Path) -> bool:
    """字幕 SRT 只保留含中文的字幕块（多语言字幕混排时去掉其他语言）。

    返回是否保留下了内容；整份都没有中文时删除文件并返回 False。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    out, idx = [], 0
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        content = [l for l in lines if "-->" not in l and not re.fullmatch(r"\d+", l.strip())]
        if not content or not any(_has_cjk(l) for l in content):
            continue
        idx += 1
        out.append(f"{idx}\n" + "\n".join(l for l in lines if not re.fullmatch(r"\d+", l.strip())))
    if not out:
        path.unlink(missing_ok=True)
        return False
    path.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    return True


def _quality_args(quality: str) -> list[str]:
    return ["-q", YUTTO_QUALITY_MAP.get(quality, YUTTO_QUALITY_MAP["best"])]


def _sessdata_args(sessdata: str) -> list[str]:
    return ["-c", sessdata] if sessdata else []


# ---------------------------------------------------------------------------
# 进度事件分流：进程内直调 yutto，从渲染器 emit 里"偷听"实时下载事件
# ---------------------------------------------------------------------------

# 每个工作线程自己的事件转发目标（工具箱可同时跑多个 B站任务，互不串线）
_EVENT_TEE = threading.local()
_tee_installed = False
# yutto CLI 从 sys.argv 读参数（进程全局），进程内并发调用会互相踩参数，
# 因此同一时刻只允许一个进程内任务；其余任务在此排队
_INPROCESS_LOCK = threading.Lock()


def _install_yutto_event_tee():
    """给 yutto 渲染器的 emit / report 打线程级分流补丁（幂等）。

    - emit：魔改版 yutto 的所有下载事件（含 DownloadProgress 实时字节）都流经
      这一个函数；子进程管道模式下事件照常产生、只是因 isatty=False 不渲染。
    - report：批量下载（收藏夹/合集/投稿）的 [k/N] 进度徽章走 report 通道，
      且在每个视频开始下载前逐个打印——这是插件侧获取「当前第 k/N 个视频」
      的唯一结构化来源（批量展开发生在下载管理器内部，不产生批量事件）。

    在原渲染前先转发给当前线程的聚合器，CLI 原行为完全不受影响。
    """
    global _tee_installed
    if _tee_installed:
        return
    from yutto.cli.event_renderer import CliApplicationEventRenderer
    if getattr(CliApplicationEventRenderer.emit, "_toolbox_tee", False):
        _tee_installed = True
        return
    _orig_emit = CliApplicationEventRenderer.emit

    def _tee_emit(self, event):
        on_event = getattr(_EVENT_TEE, "on_event", None)
        if on_event is not None:
            try:
                on_event(event)
            except Exception:  # noqa: BLE001  进度转发绝不影响下载主流程
                pass
        return _orig_emit(self, event)

    _tee_emit._toolbox_tee = True
    CliApplicationEventRenderer.emit = _tee_emit

    _orig_report = CliApplicationEventRenderer.report

    def _tee_report(self, message, level, badge, color):
        on_report = getattr(_EVENT_TEE, "on_report", None)
        if on_report is not None:
            try:
                on_report(message, level, badge, color)
            except Exception:  # noqa: BLE001
                pass
        return _orig_report(self, message, level, badge, color)

    _tee_report._toolbox_tee = True
    CliApplicationEventRenderer.report = _tee_report
    _tee_installed = True


class _YuttoProgressAggregator:
    """把 yutto 事件流折算成壳的 progress(percent, message) 回调。

    yutto 下载前已探测视频+音频流总大小，DownloadProgress 的 current/total
    即为当前条目的聚合字节数，直接可用。

    进度条语义：展示「整个任务的总体进度」——
    - 收藏夹/合集/投稿等批量任务：来自 report 通道的 [k/N] 徽章标记每个视频
      的开始（批量展开在 yutto 下载管理器内部，不产生批量事件），第 k/N 个
      视频占用整体进度的 [k-1)/N, k/N) 窗口（下载占窗口前 90%，合并占后 10%）；
    - 单视频 / 多 URL 列表（DownloadRequestQueued）：同一套窗口逻辑；
    - 无批量信号时占用整个 0-100 区间。

    进度上报限频 0.4s，阶段切换立即上报。
    """

    _MIN_INTERVAL = 0.4
    _BATCH_BADGE_RE = re.compile(r"\[(\d+)/(\d+)\]")

    def __init__(self, progress):
        self._progress = progress
        self._batch_index = 0   # 1-based
        self._batch_total = 0
        self._last_ts = 0.0
        self._last_sig = None

    def _prefix(self) -> str:
        return f"({self._batch_index}/{self._batch_total}) " if self._batch_total > 1 else ""

    def _window(self) -> tuple[float, float]:
        """当前条目在整体百分比里的 [start, end) 区间。"""
        if self._batch_total > 1 and self._batch_index > 0:
            span = 100.0 / self._batch_total
            return (self._batch_index - 1) * span, self._batch_index * span
        return 0.0, 100.0

    def _push(self, pct, msg, throttled: bool = False):
        sig = (pct, msg)
        now = time.time()
        if throttled and (sig == self._last_sig or now - self._last_ts < self._MIN_INTERVAL):
            return
        self._last_ts = now
        self._last_sig = sig
        try:
            self._progress(None if pct is None else max(0, min(99, int(pct))), msg)
        except Exception:  # noqa: BLE001
            pass

    def on_report(self, message, level, badge, color):
        """report 通道：识别批量任务的第 k/N 个视频开始下载的徽章。"""
        m = self._BATCH_BADGE_RE.fullmatch(badge or "")
        if not m:
            return
        index, total = int(m.group(1)), int(m.group(2))
        if (index, total) != (self._batch_index, self._batch_total):
            self._batch_index, self._batch_total = index, total
            self._push(self._window()[0], f"{self._prefix()}开始处理")

    def on_event(self, event):
        from yutto.core.events import (  # 延迟导入，避免无 yutto 环境下的顶层报错
            DownloadArtifactCreated, DownloadBatchStarted, DownloadItemSkipped,
            DownloadProgress, DownloadRequestQueued, DownloadStage, DownloadStageChanged,
        )
        match event:
            case DownloadBatchStarted(total=total):
                self._batch_total = total
            case DownloadRequestQueued(index=index, total=total):
                # 多 URL 列表级批量；收藏夹/合集的单 URL 展开没有此事件，
                # 由 on_report 的 [k/N] 徽章接管窗口
                if total > 1 and (index, total) != (self._batch_index, self._batch_total):
                    self._batch_index, self._batch_total = index, total
                    self._push(self._window()[0], f"{self._prefix()}开始处理")
            case DownloadStageChanged(name=stage):
                if stage == DownloadStage.DOWNLOADING:
                    self._push(None, f"{self._prefix()}下载中…")
                elif stage == DownloadStage.POSTPROCESSING:
                    ws, we = self._window()
                    self._push(ws + (we - ws) * 0.92, f"{self._prefix()}合并音视频中…")
                else:  # RESOLVING / PREPARING / WRITING_RESOURCES
                    self._push(self._window()[0], f"{self._prefix()}解析视频信息…")
            case DownloadProgress(current=current, total=total, speed_per_second=speed,
                                  phase=DownloadStage.DOWNLOADING):
                ws, we = self._window()
                frac = current / total if total > 0 else 0.0
                pct = ws + (we - ws) * 0.9 * frac
                self._push(pct, (f"{self._prefix()}下载 {current / 1048576:.1f} / "
                                 f"{total / 1048576:.1f} MB（{speed / 1048576:.1f} MB/s）"),
                           throttled=True)
            case DownloadItemSkipped() | DownloadArtifactCreated():
                pass  # 条目完成/跳过：等下一个 [k/N] 徽章或最终 100% 即可
            case _:
                pass


def _run_yutto(args: list[str], workdir: Path, allow_empty: bool = False, progress=None) -> list[Path]:
    """调用 yutto 下载，返回生成的文件列表（mp4 优先）。

    有 progress 回调时优先走进程内路径（实时字节级进度）；基础设施异常
    （如 yutto 内部结构变化导致 import 失败）自动回退 CLI 子进程模式。
    allow_empty=True 时空结果返回 [] 而不报错（字幕下载等本身可能无产物）。
    """
    ffmpeg_args = []
    ffmpeg_path = _get_ffmpeg_path()
    # 系统 PATH 里有的话 yutto 自己能找到；否则把 imageio-ffmpeg 的静态二进制显式传给它
    if ffmpeg_path and ffmpeg_path != "ffmpeg":
        ffmpeg_args = ["--ffmpeg-path", ffmpeg_path]
    full_args = [*args, *ffmpeg_args]

    if progress is not None:
        try:
            _run_yutto_inprocess(full_args, workdir, progress)
            return _collect_output_files(workdir)
        except ImportError as exc:
            logger.warning("进程内调用 yutto 不可用（%s），回退子进程模式", exc)
        # RuntimeError 是真实下载错误（登录失效/风控/网络），直接抛给用户，不回退重跑

    return _run_yutto_subprocess(full_args, workdir, allow_empty)


def _collect_output_files(workdir: Path) -> list[Path]:
    """递归收集产物文件（mp4 优先），空结果由调用方决定是否报错。"""
    files = [p for p in sorted(workdir.rglob("*")) if p.is_file()]
    files.sort(key=lambda p: 0 if p.suffix.lower() == ".mp4" else 1)
    return files


def _run_yutto_inprocess(full_args: list[str], workdir: Path, progress) -> None:
    """进程内直调 yutto CLI 入口（main），事件分流获得实时进度。

    - 事件转发：_install_yutto_event_tee 的补丁把渲染器收到的事件先喂给聚合器；
    - 日志捕获：yutto 的 Logger 走 print(stdout)，redirect 后留作报错展示；
    - SystemExit：yutto 对参数/风控等错误以 sys.exit 退出，转成 RuntimeError；
      其他异常（网络等）同样包装，避免向线程池泄漏半截状态。
    成功时静默返回；失败抛 RuntimeError（含输出尾部与风控提示）。
    """
    agg = _YuttoProgressAggregator(progress)
    _install_yutto_event_tee()
    buf = io.StringIO()
    old_argv = sys.argv
    code = 0
    try:
        with _INPROCESS_LOCK, contextlib.redirect_stdout(buf):
            _EVENT_TEE.on_event = agg.on_event
            _EVENT_TEE.on_report = agg.on_report
            sys.argv = ["yutto", *full_args, "-d", str(workdir), "--no-color", "--no-progress"]
            try:
                from yutto.__main__ import main as yutto_main
                yutto_main()
            except SystemExit as exc:  # yutto 的正常错误出口
                code = exc.code or 0
    except SystemExit as exc:  # redirect 恢复过程中抛出的退出（防御性）
        code = exc.code or 0
    finally:
        sys.argv = old_argv
        _EVENT_TEE.on_event = None
        _EVENT_TEE.on_report = None

    if code not in (0, None):
        tail = "\n".join(buf.getvalue().strip().splitlines()[-15:])
        hint = ""
        if any("未提供登录认证信息" in l or "风控校验失败" in l for l in buf.getvalue().splitlines()):
            hint = "\n\n提示：当前未登录或被 B站 风控拦截，请先使用「扫码登录B站」功能登录后重试。"
        raise RuntimeError(f"yutto 下载失败（exit {code}）：\n{tail}{hint}")
    if progress:
        progress(100, "完成")
    # 非 SystemExit 的异常（网络错误等）不在 redirect 内捕获——它们本身就是
    # 明确的失败原因，向上传播即可，与子进程模式的非零退出语义一致。


def _run_yutto_subprocess(full_args: list[str], workdir: Path, allow_empty: bool = False) -> list[Path]:
    """CLI 子进程方式调用 yutto（兜底路径；管道下无进度条，只有阶段日志）。"""
    cmd = [sys.executable, "-m", "yutto", *full_args,
           "-d", str(workdir), "--no-color", "--no-progress"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    output_lines: list[str] = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env, bufsize=1,
    )
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip()
        output_lines.append(line)
        if len(output_lines) > 400:  # 只保留尾部日志用于报错展示
            del output_lines[:200]
    proc.wait()

    if proc.returncode != 0:
        output = "\n".join(output_lines[-15:])
        hint = ""
        if any("未提供登录认证信息" in l or "风控校验失败" in l for l in output_lines):
            hint = "\n\n提示：当前未登录或被 B站 风控拦截，请先使用「扫码登录B站」功能登录后重试。"
        raise RuntimeError(f"yutto 下载失败（exit {proc.returncode}）：\n{output}{hint}")

    files = _collect_output_files(workdir)
    if not files:
        if allow_empty:
            return []
        tail = "\n".join(output_lines[-10:])
        raise RuntimeError(f"yutto 执行完成，但没有产出文件。yutto 输出：\n{tail or '(无输出)'}")
    return files


def _reject_batch_url(url: str):
    """单视频动作收到批量链接时报错，引导用户使用对应批量功能。"""
    if RE_FAVLIST.search(url):
        raise ValueError("检测到收藏夹链接，请使用「下载收藏夹」功能")
    if RE_LIST.search(url):
        raise ValueError("检测到合集/列表链接，请使用「下载合集/系列」功能")
    if RE_SPACE.search(url):
        raise ValueError("检测到 UP主空间链接，请使用「下载UP主全部投稿」功能")


def download_video(url: str, output_dir: Path, quality: str = "best", sessdata: str = "",
                   progress=None) -> list[str]:
    """下载单个 B站视频（BV / av / b23.tv 短链），返回输出文件路径列表。"""
    _reject_batch_url(url)
    if not RE_SINGLE_VIDEO.search(url):
        raise ValueError("无法识别的链接。支持：BV 号链接（https://www.bilibili.com/video/BVxxxx）、"
                         "av 号链接、b23.tv 短链接；收藏夹/合集/UP主空间请使用对应批量功能")
    args = [*_quality_args(quality), *_sessdata_args(sessdata), url]
    return [str(p) for p in _run_yutto(args, output_dir, progress=progress)]


def _batch_common(url: str, output_dir: Path, quality: str, sessdata: str, progress=None) -> list[str]:
    args = ["-b", *_quality_args(quality), *_sessdata_args(sessdata), url]
    return [str(p) for p in _run_yutto(args, output_dir, progress=progress)]


def _require_favlist(url: str):
    if not RE_FAVLIST.search(url):
        raise ValueError("请填写收藏夹链接，如 https://space.bilibili.com/261417564/favlist?fid=3233073764 "
                         "（也支持不带 fid 的「全部收藏夹」链接）")


def _require_list(url: str):
    if not RE_LIST.search(url):
        raise ValueError("请填写合集链接，如 https://space.bilibili.com/261417564/lists/12345?type=season，"
                         "或播放列表链接 https://www.bilibili.com/list/261417564?sid=12345")


def _require_space(url: str):
    if RE_FAVLIST.search(url):
        raise ValueError("检测到收藏夹链接，请使用「下载收藏夹」功能")
    if RE_LIST.search(url):
        raise ValueError("检测到合集/列表链接，请使用「下载合集/系列」功能")
    if not RE_SPACE.search(url):
        raise ValueError("请填写 UP主空间链接，如 https://space.bilibili.com/261417564")


def download_favlist(url: str, output_dir: Path, quality: str = "best", sessdata: str = "",
                     progress=None) -> list[str]:
    """批量下载整个收藏夹。"""
    _require_favlist(url)
    return _batch_common(url, output_dir, quality, sessdata, progress=progress)


def download_collection(url: str, output_dir: Path, quality: str = "best", sessdata: str = "",
                        progress=None) -> list[str]:
    """批量下载合集（type=season）/ 系列（type=series）/ 播放列表。"""
    _require_list(url)
    return _batch_common(url, output_dir, quality, sessdata, progress=progress)


def download_space(url: str, output_dir: Path, quality: str = "best", sessdata: str = "",
                   progress=None) -> list[str]:
    """批量下载 UP主空间的全部投稿视频。"""
    _require_space(url)
    return _batch_common(url, output_dir, quality, sessdata, progress=progress)


def _transcode_to_mp3(src: Path, dst: Path):
    """将 m4a/aac 音频转码为 192kbps mp3。"""
    result = subprocess.run(
        [*_ffmpeg_exe(), "-y", "-i", str(src),
         "-codec:a", "libmp3lame", "-b:a", "192k", str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0 or not dst.exists():
        tail = "\n".join((result.stderr or "").strip().splitlines()[-10:])
        raise RuntimeError(f"mp3 转码失败：\n{tail}")


def download_audio(url: str, output_dir: Path, fmt: str = "mp3", sessdata: str = "") -> str:
    """下载 B站视频的音轨：m4a 直接无损拷贝，mp3 由 ffmpeg 转 192k。返回输出文件路径。"""
    fmt = (fmt or "mp3").strip().lower()
    if fmt not in ("mp3", "m4a"):
        fmt = "mp3"

    # 统一先以 m4a（aac copy，无转码损耗）落地，需要 mp3 时再转码
    args = ["--audio-only", "--no-danmaku", "--output-format-audio-only", "m4a",
            *_sessdata_args(sessdata), url]
    files = _run_yutto(args, output_dir)
    m4a_files = [p for p in files if p.suffix.lower() in (".m4a", ".aac")]
    if not m4a_files:
        raise RuntimeError("yutto 未产出音频文件")
    m4a = m4a_files[0]

    if fmt == "m4a":
        return str(m4a)

    mp3 = m4a.with_suffix(".mp3")
    _transcode_to_mp3(m4a, mp3)
    m4a.unlink()
    return str(mp3)


def get_subtitle(url: str, output_dir: Path, sessdata: str = "") -> str:
    """下载 B站视频的 CC 字幕（yutto --subtitle-only），SRT 结果只保留中文。

    注意：B站字幕接口需要登录，匿名请求拿不到字幕——此时返回「无字幕」提示文件，
    引导用户扫码登录或改用 Whisper 语音转写。
    """
    args = ["--subtitle-only", *_sessdata_args(sessdata), url]
    files = _run_yutto(args, output_dir, allow_empty=True)
    srt_files = sorted(p for p in files if p.suffix.lower() == ".srt")

    title = get_video_title(url)

    # SRT 只保留中文：各语言字幕分别过滤后合并，序号全局重排
    had_subtitles = False
    merged_blocks: list[str] = []
    for srt_file in srt_files:
        had_subtitles = True
        if _filter_srt_chinese_only(srt_file):
            text = srt_file.read_text(encoding="utf-8", errors="replace").strip()
            merged_blocks.extend(b for b in re.split(r"\n\s*\n", text) if b.strip())
        srt_file.unlink(missing_ok=True)

    if merged_blocks:
        merged = output_dir / f"{title}_字幕.srt"
        numbered = [f"{i}\n{block}" for i, block in enumerate(merged_blocks, 1)]
        merged.write_text("\n\n".join(numbered) + "\n", encoding="utf-8")
        return str(merged)

    if had_subtitles:
        no_sub_file = output_dir / f"{title}_无中文字幕.txt"
        no_sub_file.write_text(
            f"视频《{title}》找到了字幕，但没有中文内容（已按「SRT 只保留中文」过滤，其他语言未保留）。\n"
            f"如需原文文字，可搭配 Whisper 使用：\n"
            f"1. 先用「下载音频」功能下载音频\n"
            f"2. 再用 Whisper 将音频转为文字\n"
            f"链接：{url}\n",
            encoding="utf-8-sig",
        )
        return str(no_sub_file)

    no_sub_file = output_dir / f"{title}_无字幕.txt"
    no_sub_file.write_text(
        f"视频《{title}》没有下载到 CC 字幕。\n"
        f"常见原因：B站字幕接口需要登录态，匿名请求无法获取字幕，"
        f"请先使用「扫码登录B站」功能登录后重试；也可能该视频本身没有 CC 字幕。\n"
        f"如需语音转文字，可搭配 Whisper 使用：\n"
        f"1. 先用「下载音频」功能下载音频\n"
        f"2. 再用 Whisper 将音频转为文字\n"
        f"链接：{url}\n",
        encoding="utf-8-sig",
    )
    return str(no_sub_file)


# ---------------------------------------------------------------------------
# 扫码登录（前端弹窗直接展示二维码 + 轮询状态；凭证写入 yutto 认证文件，
# 登录后所有下载自动生效，用户无需接触 cookie）
# ---------------------------------------------------------------------------

# B站通行证二维码登录接口（与 yutto login.py 使用的一致）
QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
# 轮询返回的 data.code 状态码
QR_NOT_SCANNED = 86101
QR_SCANNED = 86090
QR_EXPIRED = 86038
QR_CONFIRMED = 0
# 二维码有效期（B站约 3 分钟）
QR_MAX_AGE_SECONDS = 180
# 临近过期阈值：生成二维码后超过这个时长、且仍无人扫码时，后端主动返回 refresh
# 要求前端重新生成，避免被动等到 180s 真正过期那一刻才换码（用户正要扫就失效）。
# 已扫码的 scanned 分支已在上文优先返回，不会打断用户正在手机上进行的确认操作。
QR_REFRESH_THRESHOLD_SECONDS = 150

# 扫码会话状态（token 在内存中；服务重启后前端重新生成二维码即可）
_BILI_LOGIN: dict = {"token": "", "created_at": 0.0}


def _bilibili_api_get(url: str, params: dict, cookie: str = None) -> dict:
    """调用 B站 Web API（GET），返回 JSON。"""
    query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    req = urllib.request.Request(f"{url}?{query}", headers={
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        **({"Cookie": cookie} if cookie else {}),
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def login_start() -> dict:
    """生成登录二维码，返回 {"image": dataURL} 供前端弹窗直接展示。"""
    import base64
    import io

    import segno  # yutto 自带的二维码库

    payload = _bilibili_api_get(QR_GENERATE_API, {"source": "main-fe-header"})
    data = payload.get("data") or {}
    login_url, qr_key = data.get("url"), data.get("qrcode_key")
    if payload.get("code") != 0 or not login_url or not qr_key:
        raise RuntimeError(f"获取登录二维码失败：{payload}")

    buf = io.BytesIO()
    segno.make(login_url).save(buf, kind="png", scale=8, border=2)
    image = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    _BILI_LOGIN.update({"token": qr_key, "created_at": time.time()})
    return {"image": image}


def login_poll() -> dict:
    """查询一次扫码状态，返回 {"status": waiting/scanned/confirmed/expired/error, "message": str}。

    confirmed 时凭证已写入 yutto 认证文件，所有下载功能自动生效。
    """
    token = _BILI_LOGIN.get("token")
    if not token:
        return {"status": "error", "message": "请先重新打开扫码登录生成二维码"}
    if time.time() - _BILI_LOGIN["created_at"] > QR_MAX_AGE_SECONDS:
        return {"status": "expired", "message": "二维码已过期"}

    payload = _bilibili_api_get(QR_POLL_API, {"qrcode_key": token, "source": "main-fe-header"})
    data = payload.get("data") or {}
    code = data.get("code")

    if code == QR_CONFIRMED:
        # 凭证在 crossDomain 响应的 Set-Cookie 里（旧版在 URL 参数里，函数内已做兜底）
        sessdata, bili_jct = _exchange_qr_ticket(data.get("url", ""))
        if not sessdata:
            return {"status": "error",
                    "message": "登录已确认，但未能提取登录凭证，请重新扫码再试一次"}
        from yutto.auth import default_auth_file, save_auth
        save_auth(default_auth_file(), "default", sessdata, bili_jct)
        nickname = _check_login(sessdata)
        _BILI_LOGIN.update({"token": "", "created_at": 0.0})
        msg = f"登录成功，当前账号：{nickname}" if nickname else "登录成功"
        return {"status": "confirmed", "message": msg + "，所有下载功能已自动生效"}
    if code == QR_SCANNED:
        return {"status": "scanned", "message": "已扫码，请在手机上确认"}
    if code == QR_EXPIRED:
        return {"status": "expired", "message": "二维码已过期"}
    # 仍未扫码且临近过期：主动要求前端刷新。已扫码的 scanned 分支已在上文优先返回，
    # 因此这里不会打断用户正在手机上进行的确认操作。
    if (time.time() - _BILI_LOGIN["created_at"]) > QR_REFRESH_THRESHOLD_SECONDS:
        return {"status": "refresh", "message": "二维码即将过期，自动刷新中…"}
    return {"status": "waiting", "message": "等待扫码…"}


def _exchange_qr_ticket(redirect_url: str) -> tuple:
    """请求扫码确认后的 crossDomain 地址，提取登录凭证 SESSDATA / bili_jct。

    B站现在的扫码确认返回的是 ticket 换凭证地址（凭证在响应 Set-Cookie 里），
    必须真实请求一次才能拿到；旧版「凭证直接拼在 URL 参数里」的行为保留为兜底。
    crossDomain 会跨域为 .bilibili.com 下发 cookie，须放宽 cookie 域校验才能收进 jar。
    """
    class _LaxPolicy(http.cookiejar.DefaultCookiePolicy):
        def set_ok_domain(self, cookie, request):
            return True

        def return_ok_domain(self, cookie, request):
            return True

    jar = http.cookiejar.CookieJar(policy=_LaxPolicy())
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    final_url = redirect_url
    try:
        req = urllib.request.Request(redirect_url, headers={
            "User-Agent": _UA,
            "Referer": "https://www.bilibili.com/",
        })
        with opener.open(req, timeout=15) as resp:
            final_url = resp.geturl()
    except Exception as exc:  # noqa: BLE001
        logger.warning("请求登录凭证地址失败：%s", exc)

    sessdata = bili_jct = None
    for c in jar:
        if c.name == "SESSDATA" and not sessdata:
            sessdata = c.value
        elif c.name == "bili_jct" and not bili_jct:
            bili_jct = c.value

    if not sessdata:
        # 旧版兜底：凭证直接在返回 URL 的参数里
        query = parse_qs(urlparse(final_url).query)
        sessdata = (query.get("SESSDATA") or [None])[0]
        bili_jct = bili_jct or (query.get("bili_jct") or [None])[0]
    return sessdata, bili_jct


def _check_login(sessdata: str) -> str:
    """用 SESSDATA 查询当前登录账号昵称，未登录或失效返回空串。"""
    try:
        data = _bilibili_api_get("https://api.bilibili.com/x/web-interface/nav", {}, cookie=f"SESSDATA={sessdata}")
        d = data.get("data") or {}
        return d.get("uname") if d.get("isLogin") else ""
    except Exception:  # noqa: BLE001
        return ""


def login_logout(output_dir: Path) -> str:
    """清除 yutto 认证文件中的登录凭证。"""
    from yutto.auth import default_auth_file, remove_auth
    auth_file = default_auth_file()
    removed = False
    try:
        removed = remove_auth(auth_file, "default")
    except Exception:  # noqa: BLE001
        pass
    f = output_dir / ("已退出登录.txt" if removed else "当前未登录.txt")
    f.write_text(
        "已清除本机保存的B站登录凭证。\n" if removed
        else "本机没有保存过B站登录凭证，无需退出。\n",
        encoding="utf-8-sig",
    )
    return str(f)
