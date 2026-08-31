"""抖音单视频抓取：链接/ID -> 签名调用 web detail 接口 -> 无水印视频 + 音频提取。

依赖：系统自带的 curl（绕过 Python 网络库的 TLS 指纹/出口差异，直接复用真实浏览器通道）、
gmssl（签名算法，见同目录 abogus.py，源自 f2 项目 Apache-2.0）、
以及系统 ffmpeg（音频提取用）。

支持输入：
  - 完整网页链接（含 modal_id=xxx、/video/xxx、/note/xxx）
  - 短链（https://v.douyin.com/xxxx/）
  - 裸视频 ID（15~20 位数字）

流程（大白话）：
  1. 从各种链接里抠出 19 位视频 ID；
  2. 先访问一次 douyin.com 拿游客身份凭证（ttwid Cookie）；
  3. 用 ABogus 算法给请求"签名"——抖音用它判断请求是不是真浏览器发的；
  4. 调 detail 接口拿视频元数据（标题、作者、播放地址）；
  5. 下载无水印视频；音频则用 ffmpeg 从视频里原样抽出音轨。
"""
import json
import random
import re
import shutil
import string
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from abogus import ABogus, BrowserFingerprintGenerator  # noqa: I001

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
PLAY_URL = "https://www.iesdouyin.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"

_ID_PATTERNS = [
    re.compile(r"modal_id=(\d{10,25})"),
    re.compile(r"/(?:video|note)/(\d{10,25})"),
    re.compile(r"\b(\d{15,20})\b"),
]

_DEFAULT_QUERY = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "pc_libra_divert": "Windows",
    "version_code": "290100",
    "version_name": "29.1.0",
    "cookie_enabled": "true",
    "screen_width": "1536",
    "screen_height": "864",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "139.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "139.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "16",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "200",
    "support_h265": "1",
    "support_dash": "1",
    "uifid": "",
}


def _curl_present() -> bool:
    return shutil.which("curl") is not None


def _fake_ms_token() -> str:
    """生成一个随机 msToken（接口对游客态并不严格校验其真实性，长度凑够 184 即可）。"""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(182)) + "=="


def _build_ttwid() -> str:
    """访问 douyin.com 拿到游客 ttwid；拿不到就向字节跳动注册端点申请一个。"""
    if not _curl_present():
        raise RuntimeError("未找到 curl，无法访问抖音接口（音频提取不依赖它，但解析视频需要）")
    # 直接调注册端点拿 ttwid
    import os as _os
    import tempfile
    hdr = tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False)
    hdr.close()
    try:
        _curl([
            "-s", "-o", _os.devnull, "-D", hdr.name,
            "-H", "Content-Type: application/json",
            "-d", json.dumps({
                "region": "cn", "aid": 1768, "needFid": False,
                "service": "www.ixigua.com", "migrate_info": {"ticket": "", "source": "node"},
                "cbUrlProtocol": "https", "union": True,
            }),
            "https://ttwid.bytedance.com/ttwid/union/register/",
        ])
        out = Path(hdr.name).read_text(encoding="utf-8", errors="replace")
    finally:
        Path(hdr.name).unlink(missing_ok=True)
    m = re.search(r"ttwid=([^;,\r\n]+)", out or "")
    return m.group(1).strip() if m else ""


def _curl(args: list, as_text: bool = True) -> str:
    """统一封装 curl 调用（带 -s，跟随重定向）。"""
    cmd = ["curl", "-s", "-L", "--compressed"] + args
    proc = subprocess.run(cmd, capture_output=True, text=as_text,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"curl 执行失败（exit {proc.returncode}）：{proc.stderr[:200]}")
    return proc.stdout


def extract_aweme_id(link: str) -> str:
    """从各种形态的输入里提取视频 ID。"""
    text = (link or "").strip()
    if not text:
        raise ValueError("链接/ID 不能为空")
    if "v.douyin.com" in text:
        try:
            redir = _curl(["-s", "-o", "/dev/null", "-w", "%{redirect_url}",
                           "-H", f"User-Agent: {UA}", text])
            if redir:
                text = redir
        except Exception:
            pass
    for pattern in _ID_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    raise ValueError(f"无法从输入中识别视频 ID：{link}")


def _pick_play_addr(detail: dict) -> str:
    """优先选最高码率的播放地址，其次退回 play_addr。"""
    video = detail.get("video") or {}

    best_uri, best_kbps = "", 0
    for entry in video.get("bit_rate") or []:
        addr = entry.get("play_addr") or {}
        uri = addr.get("uri") or ""
        kbps = int(entry.get("bit_rate") or 0)
        if uri and kbps > best_kbps:
            best_uri, best_kbps = uri, kbps
    if best_uri:
        return best_uri

    uri = (video.get("play_addr") or {}).get("uri") or ""
    if not uri:
        raise RuntimeError("未找到视频播放地址（可能是图文笔记或直播回放，暂不支持）")
    return uri


def _signed_detail(aweme_id: str) -> dict:
    """构造默认参数 -> ABogus 签名 -> 用 curl 调 detail 接口，返回 aweme_detail 字典。"""
    ttwid = _build_ttwid()
    params = dict(_DEFAULT_QUERY)
    params["aweme_id"] = aweme_id
    params["msToken"] = _fake_ms_token()
    query = urlencode(params)

    fp = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
    signer = ABogus(fp=fp, user_agent=UA)
    signed_query, _ab, signed_ua, _body = signer.generate_abogus(query, "")
    cookie = f"ttwid={ttwid}" if ttwid else ""
    headers = [
        "-H", f"User-Agent: {signed_ua}",   # 签名与 UA 绑定，必须用签名时返回的 UA
        "-H", "Referer: https://www.douyin.com/",
        "-H", "Accept: */*",
    ]
    if cookie:
        headers += ["-H", f"Cookie: {cookie}"]

    body = _curl(headers + [f"{DETAIL_URL}?{signed_query}"]).strip()
    if not body:
        raise RuntimeError("detail 接口无有效响应，可能触发风控，请稍后重试")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("detail 接口返回非 JSON（反爬拦截），请稍后重试") from exc

    detail = data.get("aweme_detail") or {}
    if not detail:
        reason = (data.get("filter_detail") or {}).get("filter_reason")
        hint = f"（过滤原因: {reason}）" if reason else ""
        raise RuntimeError(f"未获取到视频详情{hint}，视频可能已删除、为私密作品或被风控拦截")
    return detail


def fetch_video_info(aweme_id: str) -> dict:
    """返回 {title, author, video_url}。"""
    detail = _signed_detail(aweme_id)
    title = (detail.get("desc") or "").strip() or f"douyin_{aweme_id}"
    author = (detail.get("author") or {}).get("nickname") or "unknown"
    uri = _pick_play_addr(detail)
    return {"title": title, "author": author, "video_url": PLAY_URL.format(uri=uri)}


def _safe_filename(name: str, max_len: int = 60) -> str:
    """文件名净化：去掉路径分隔符与 Windows 非法字符。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._")
    return (cleaned or "douyin_video")[:max_len]


def download_video(aweme_id: str, workdir: Path) -> Path:
    """下载无水印视频，返回本地 mp4 路径。"""
    info = fetch_video_info(aweme_id)
    target = workdir / f"{_safe_filename(info['title'])}.mp4"
    headers = [
        "-H", f"User-Agent: {UA}",
        "-H", "Referer: https://www.douyin.com/",
    ]
    _curl(headers + ["-o", str(target), info["video_url"]])
    size = target.stat().st_size if target.exists() else 0
    if size < 10_000:
        raise RuntimeError(f"视频下载不完整（{size} 字节），请重试")
    return target


def _resolve_ffmpeg() -> str:
    """优先用 imageio-ffmpeg 自带的静态二进制（开箱即用、无需系统安装），
    回退到 PATH 中的系统 ffmpeg；两者皆无则给出明确报错。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    sys_ff = shutil.which("ffmpeg")
    if not sys_ff:
        raise RuntimeError(
            "未找到 ffmpeg：请安装 imageio-ffmpeg（pip install imageio-ffmpeg）"
            "或将系统 ffmpeg 加入 PATH（音频提取依赖它）"
        )
    return sys_ff


def extract_audio(mp4_path: Path, out_dir: Path, fmt: str = "flac") -> Path:
    """用 ffmpeg 从视频中提取音频。

    - m4a  : 无损搬运原始 AAC 流（-c:a copy，文件最小，音质与源一致）
    - flac : 无损容器封装（音质与源一致，文件较大，便于进音乐库统一管理）
    - mp3  : 有损重压缩（仅兼容场景使用）
    """
    ffmpeg = _resolve_ffmpeg()
    fmt = (fmt or "flac").lower().lstrip(".")
    if fmt not in ("m4a", "flac", "mp3"):
        raise ValueError(f"不支持的音频格式: {fmt}（仅支持 m4a/flac/mp3）")

    out_path = out_dir / f"{mp4_path.stem}.{fmt}"
    if fmt == "m4a":
        cmd = [ffmpeg, "-y", "-i", str(mp4_path), "-vn", "-c:a", "copy", str(out_path)]
    elif fmt == "flac":
        cmd = [ffmpeg, "-y", "-i", str(mp4_path), "-vn", "-c:a", "flac", str(out_path)]
    else:
        cmd = [ffmpeg, "-y", "-i", str(mp4_path), "-vn", "-c:a", "libmp3lame", "-q:a", "0",
               str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out_path.exists():
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("ffmpeg 音频提取失败：" + " | ".join(tail))
    return out_path
