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
# 原画探测端点：ratio=default 时该端点会 302 到「上传原片」（bit_rate 转码
# 阶梯之外，实测最高转码档可比原片小数倍）。参考 douyin-downloader 的
# original 画质实现。
ORIGINAL_PLAY_URL = (
    "https://www.iesdouyin.com/aweme/v1/play/?video_id={uri}"
    "&ratio=default&line=0&is_play_url=1&watermark=0&source=PackSourceEnum_PUBLISH"
)
# 原画探测超时（秒）：探测是锦上添花，绝不拖慢主下载流程
_ORIGINAL_PROBE_TIMEOUT = 10

# 固定分辨率档的长边参考值：兼容只有 width 的旧响应，用最近档位估算短边
_QUALITY_TARGET_WIDTH = {
    "1440p": 2560, "1080p": 1920, "720p": 1280,
    "540p": 960, "480p": 854, "360p": 640,
}

# 插件目录（douyin_download/）下的可选登录态 Cookie 文件：
# 用户把浏览器里的抖音登录 Cookie 一次性粘进 cookies.txt，解析与下载共用同一身份，
# 受限视频的成功率与你在浏览器里看到的预览一致。文件已被 .gitignore 忽略，不会误提交。
PLUGIN_DIR = Path(__file__).resolve().parent.parent
COOKIES_FILE = PLUGIN_DIR / "cookies.txt"


class DouyinAuthError(RuntimeError):
    """抖音登录态 / Cookie 失效或被风控导致的失败，需要用户重新提供 Cookie。

    与普通 RuntimeError（视频已删除、私密作品、网络抖动等）区分开，
    便于在 download_video 里精准判断「是不是 Cookie 不行了」从而提示用户。
    """

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


def _real_ms_token() -> str:
    """尝试从抖音 mssdk 接口获取真实 msToken，失败时回落随机 token。

    参考 douyin-downloader / F2 的实现：调 mssdk 注册接口，
    从 Set-Cookie 响应头提取 msToken。真实 msToken 的防爬权重更高。
    """
    if not _curl_present():
        return _fake_ms_token()
    import os as _os
    import tempfile
    hdr = tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False)
    hdr.close()
    try:
        _curl([
            "-s", "-o", _os.devnull, "-D", hdr.name,
            "-H", "Content-Type: application/json",
            "-H", f"User-Agent: {UA}",
            "-d", json.dumps({
                "magic": 1,
                "version": 1,
                "dataType": 1,
                "strData": "",
                "ulr": "",
                "tspFromClient": int(time.time() * 1000),
            }),
            "https://mssdk.bytedance.com/api/v2/sdk/device_register",
        ])
        out = Path(hdr.name).read_text(encoding="utf-8", errors="replace")
        m = re.search(r"msToken=([^;,\r\n]+)", out or "")
        token = m.group(1).strip() if m else ""
        if token and len(token) >= 160:
            return token
    except Exception:
        pass
    finally:
        Path(hdr.name).unlink(missing_ok=True)
    return _fake_ms_token()


def _fake_ms_token() -> str:
    """生成一个随机 msToken（长度凑够 184，作为 real_ms_token 的回落）。"""
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


def _load_cookie(explicit: str = "") -> str:
    """取得用于解析/下载的抖音 Cookie，优先级：
    1) 调用方显式传入（UI 的 cookie 输入框）；
    2) 插件目录下的 cookies.txt（用户一次性粘贴，长期复用）；
    3) 空串 —— 此时 _signed_detail 回落到游客 ttwid（现有行为）。
    """
    explicit = (explicit or "").strip()
    if explicit:
        return explicit
    try:
        raw = COOKIES_FILE.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    # 跳过空行与 # 开头的注释行，取第一行有效内容
    lines = [ln.strip() for ln in raw.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    return lines[0] if lines else ""


def _save_cookie(cookie: str) -> None:
    """把可用的 Cookie 持久化到 cookies.txt，供下次静默复用。

    仅在「用户显式填写、且本次下载成功」时调用——也就是把用户刚验证过的好
    Cookie 存下来，之后即使不填 cookie 框也能自动用。用临时文件原子替换，避免
    写入到一半被中断留下半截 Cookie。写入失败不影响本次下载结果（静默忽略）。
    """
    cookie = (cookie or "").strip()
    if not cookie:
        return
    try:
        tmp = COOKIES_FILE.with_name(COOKIES_FILE.name + ".tmp")
        tmp.write_text(cookie + "\n", encoding="utf-8")
        tmp.replace(COOKIES_FILE)
    except OSError:
        pass


def _invalidate_cookie() -> None:
    """本地 Cookie 已失效时，清除保存的 cookies.txt。

    避免下次运行又悄悄拿这个死 Cookie 去请求、反复失败。删除后下次会回落到游客态，
    并把「请重新填写 Cookie」的提示交给用户。文件不存在也不报错。
    """
    try:
        COOKIES_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _curl(args: list, as_text: bool = True) -> str:
    """统一封装 curl 调用（带 -s，跟随重定向，自动重试 2 次）。"""
    # 提取最后一条 URL 参数用于错误定位
    url_hint = ""
    for arg in reversed(args):
        if arg.startswith("http"):
            url_hint = arg[:80]
            break
    cmd = ["curl", "-s", "-L", "--compressed", "--retry", "2"] + args
    proc = subprocess.run(cmd, capture_output=True, text=as_text,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        stdout = (proc.stdout or "").strip()[:200] if not stderr else ""
        detail = stderr or stdout or "无详细错误信息"
        raise RuntimeError(
            f"请求抖音接口失败（exit {proc.returncode}，{detail}）。\n"
            f"通常是网络原因，请稍后重试。如果持续出现，请检查网络连接或尝试使用代理。"
        )
    return proc.stdout


def _download_file(url: str, headers: list, target: Path, expected_size: int = 0,
                   progress=None, label: str = "下载中") -> None:
    """流式下载大文件并上报真实进度（curl 落盘 + 监控线程读文件大小）。

    进度显示形式：expected_size 已知时上报百分比 +「已下载 x/y MB」；
    未知时（报不出总大小）只报「已下载 x MB」，前端进度条保持未知态但
    文字仍在走。curl 失败仍抛 RuntimeError，与 _curl 行为一致。

    放在独立函数而不塞进 _curl：小请求（接口/图片）不需要监控线程的开销。
    """
    import os as _os
    import tempfile
    import time as _time
    err = tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False)
    err.close()
    with open(err.name, "wb") as err_fh:
        proc = subprocess.Popen(
            ["curl", "-s", "-S", "-L", "--compressed", "--retry", "2",
             "-o", str(target)] + headers + [url],
            stdout=subprocess.DEVNULL, stderr=err_fh,
        )
        last_pct = -1
        while proc.poll() is None:
            _time.sleep(0.5)
            if progress is None or not target.exists():
                continue
            try:
                got = target.stat().st_size
            except OSError:
                continue
            if expected_size > 0:
                pct = min(99, int(got * 100 / expected_size))
                if pct != last_pct:
                    last_pct = pct
                    # 分母取 max：预估大小偶有偏差（如 data_size 略小于实际），
                    # 避免"9.6 / 8.3 MB"这种观感 bug
                    total_show = max(expected_size, got)
                    progress(percent=pct, message=(
                        f"{label} {got / 1048576:.1f} / {total_show / 1048576:.1f} MB"))
            else:
                progress(percent=None, message=f"{label} 已下载 {got / 1048576:.1f} MB")
        ret = proc.wait()
    stderr_tail = Path(err.name).read_text(encoding="utf-8", errors="replace").strip()[:200]
    Path(err.name).unlink(missing_ok=True)
    if ret != 0:
        raise RuntimeError(
            f"下载失败（exit {ret}，{stderr_tail or '无详细错误信息'}）。\n"
            f"通常是网络原因，请稍后重试。"
        )
    if progress:
        progress(percent=100, message=f"{label}完成")


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


def _resolution_metrics(entry: dict, addr: dict) -> tuple:
    """返回 (短边, 总像素)：竖屏取宽、横屏取高，天然通吃两种方向。

    旧响应可能只有 width（横屏长边尺寸），按最近档位估算短边。
    """
    try:
        width = int(addr.get("width") or entry.get("width") or 0)
        height = int(addr.get("height") or entry.get("height") or 0)
    except (TypeError, ValueError):
        return 0, 0
    if width > 0 and height > 0:
        return min(width, height), width * height
    long_edge = max(width, height)
    if long_edge <= 0:
        return 0, 0
    nearest = min(_QUALITY_TARGET_WIDTH.items(), key=lambda kv: abs(kv[1] - long_edge))[0]
    short_edge = int(nearest[:-1])
    return short_edge, long_edge * short_edge


def _pick_play_addr(detail: dict, quality: str = "highest") -> tuple:
    """从 video.bit_rate 多档率中按目标画质挑选播放地址，返回 (uri, 该档文件大小)。

    - "highest"（默认）      分辨率最高的档，同分辨率按码率决胜
    - "1080p"/"720p"/…       画面短边最接近目标值的档，同距离按码率决胜，
                             匹配不到时自动落到最接近的可用档
    - "lowest"               码率最小的档（省流量）

    文件大小（play_addr.data_size）用于原画探测时比大小——存在超分重编码
    档大于原片的反例，盲选原画反而降质。bit_rate 阶梯为空时退回 play_addr。
    """
    video = detail.get("video") or {}

    def _size_of(addr: dict) -> int:
        try:
            return int(addr.get("data_size") or 0)
        except (TypeError, ValueError):
            return 0

    entries = []
    for entry in video.get("bit_rate") or []:
        if not isinstance(entry, dict):
            continue
        addr = entry.get("play_addr")
        if not isinstance(addr, dict) or not addr.get("uri"):
            continue
        try:
            br = int(entry.get("bit_rate") or 0)
        except (TypeError, ValueError):
            br = 0
        short_edge, pixels = _resolution_metrics(entry, addr)
        entries.append((br, short_edge, pixels, addr, _size_of(addr)))

    if entries:
        q = (quality or "highest").strip().lower()
        if q == "lowest":
            entries.sort(key=lambda t: (t[0], t[2]))
        elif q in _QUALITY_TARGET_WIDTH:
            target = int(q[:-1])
            entries.sort(key=lambda t: (abs(t[1] - target), -t[0], -t[2]))
        else:  # highest / 未知值
            entries.sort(key=lambda t: (-t[2], -t[0], -t[1]))
        addr = entries[0][3]
        return addr.get("uri") or "", entries[0][4]

    addr = video.get("play_addr") or {}
    uri = addr.get("uri") or ""
    if not uri:
        raise RuntimeError("未找到视频播放地址（可能是图文笔记或直播回放，暂不支持）")
    return uri, _size_of(addr)


def _is_paid_content(detail: dict) -> bool:
    """付费/会员作品跳过原画探测：其 play_addr 是明文试看渲染版，
    ratio=default 探到的也是同一份资产；真正的全长正片是 CENC 加密密文，
    置顶下载只会得到一份播放器解不开的文件。"""
    charge = detail.get("charge_info") if isinstance(detail, dict) else None
    return isinstance(charge, dict) and bool(charge.get("is_charge_content"))


def _probe_original(uri: str, headers: list, need_set_cookie: bool = False):
    """Range 取 1 字节探测「上传原片」直连地址，返回 (302 落点 URL, 文件总大小)。

    流程（参考 douyin-downloader）：
      1. 以 ratio=default 调 play 端点，该请求会 302 到上传原片的 CDN 直链；
      2. 带 Range: bytes=0-0 只取 1 字节，从 Content-Range 读出文件总大小；
      3. content-type 必须是 video/*——WAF 拦截页等 200+HTML 不得误判为原画。

    探测是 best-effort：任何失败（网络、WAF 403、端点变更）都返回 None，
    由调用方退回最高转码档，绝不中断主下载流程。
    """
    url = ORIGINAL_PLAY_URL.format(uri=uri)
    if need_set_cookie:
        # 受限作品官方 App 会附带此标记，缺失时 play 端点会拒绝
        url += "&ss_is_p_v_ss=1"
    import os as _os
    import tempfile
    hdr = tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False)
    hdr.close()
    try:
        proc = subprocess.run(
            ["curl", "-s", "-L", "--max-time", str(_ORIGINAL_PROBE_TIMEOUT),
             "-o", _os.devnull, "-D", hdr.name, "-w", "%{url_effective}",
             "-H", "Range: bytes=0-0"] + headers + [url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        final_url = (proc.stdout or "").strip()
        if proc.returncode != 0 or not final_url.startswith("http"):
            return None
        out = Path(hdr.name).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    finally:
        Path(hdr.name).unlink(missing_ok=True)

    # -L 跟随重定向后头文件里有多段 HTTP 块，只认最后一段（最终响应）
    blocks = re.split(r"(?m)^HTTP/", out)
    last = blocks[-1].strip() if blocks else ""
    if not last:
        return None
    m = re.match(r"[\d.]+\s+(\d{3})", last)
    if not m or m.group(1) not in ("200", "206"):
        return None
    content_type = content_range = content_length = ""
    for line in last.splitlines():
        low = line.lower()
        if low.startswith("content-type:"):
            content_type = line.split(":", 1)[1].strip().lower()
        elif low.startswith("content-range:"):
            content_range = line.split(":", 1)[1].strip()
        elif low.startswith("content-length:"):
            content_length = line.split(":", 1)[1].strip()
    if not content_type.startswith("video/"):
        return None
    raw = content_range.rsplit("/", 1)[-1] if "/" in content_range else content_length
    try:
        total = int(raw or 0)
    except ValueError:
        total = 0
    if total <= 0:
        return None
    return final_url, total


def _parse_cookie_value(cookie_str: str, key: str) -> str:
    """从 Cookie 字符串中解析指定 key 的值。"""
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return ""


def _signed_detail(aweme_id: str, cookie: str = "") -> dict:
    """构造默认参数 -> ABogus 签名 -> 用 curl 调 detail 接口，返回 aweme_detail 字典。

    参考 douyin-downloader 的做法：
      - msToken 优先从 Cookie 中提取（扫码登录后 Cookie 自带 msToken），
        与请求参数中的 msToken 保持一致，避免被风控识别为伪造请求
      - Cookie 中无 msToken 时才生成真实/随机 token
      - 尝试多个 AID（6383 → 1128）
      - 重试 3 次 + 指数退避
    """
    cookie = cookie or _load_cookie()

    # msToken 优先从 Cookie 中提取（与 douyin-downloader 一致）
    cookie_ms_token = _parse_cookie_value(cookie, "msToken")
    ms_token = cookie_ms_token if cookie_ms_token else _real_ms_token()

    # ttwid 同样优先从 Cookie 中提取
    ttwid = _parse_cookie_value(cookie, "ttwid") or _build_ttwid()

    for aid in ("6383", "1128"):
        for attempt in range(3):
            try:
                params = dict(_DEFAULT_QUERY)
                params["aweme_id"] = aweme_id
                params["aid"] = aid
                params["msToken"] = ms_token
                query = urlencode(params)

                fp = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
                signer = ABogus(fp=fp, user_agent=UA)
                signed_query, _ab, signed_ua, _body = signer.generate_abogus(query, "")

                if cookie:
                    cookie_header = cookie
                else:
                    cookie_header = f"ttwid={ttwid}" if ttwid else ""
                headers = [
                    "-H", f"User-Agent: {signed_ua}",
                    "-H", "Referer: https://www.douyin.com/",
                    "-H", "Accept: */*",
                ]
                if cookie_header:
                    headers += ["-H", f"Cookie: {cookie_header}"]

                body = _curl(headers + [f"{DETAIL_URL}?{signed_query}"]).strip()
                if not body:
                    raise RuntimeError("空响应（可能触发风控），重试中…")

                try:
                    data = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("detail 接口返回非 JSON（反爬拦截），重试中…") from exc

                detail = data.get("aweme_detail") or {}
                if detail:
                    return detail

                # 有过滤原因 → 换 AID 或重试
                reason = (data.get("filter_detail") or {}).get("filter_reason")
                if reason:
                    raise DouyinAuthError(
                        f"未获取到视频详情（过滤原因: {reason}），抖音登录态可能已失效或被风控拦截"
                    )
                # 无过滤原因也无 detail → 视频没了，尝试下一个 AID
                if aid == "6383":
                    break  # 换 aid=1128 再试
                raise RuntimeError("未获取到视频详情，视频可能已删除或为私密作品")

            except DouyinAuthError:
                raise  # 直接透传登录态错误，不重试
            except RuntimeError:
                if attempt < 2:
                    import time as _time
                    _time.sleep(1.5 * (attempt + 1))
                    continue
                if aid == "6383":
                    break  # 换 aid=1128 再试
                raise

    raise RuntimeError("获取视频详情失败，请稍后重试")


def _iter_gallery_items(detail: dict) -> list:
    """识别图文作品并返回图片条目列表（参考 douyin-downloader 的判断字段）。

    新版作品放在 image_post_info.images / image_list，旧版直接在 images / image_list。
    """
    if not isinstance(detail, dict):
        return []
    image_post = detail.get("image_post_info")
    if isinstance(image_post, dict):
        for key in ("images", "image_list"):
            candidate = image_post.get(key)
            if isinstance(candidate, list) and candidate:
                return candidate
    images = detail.get("images") or detail.get("image_list") or []
    return images if isinstance(images, list) else []


def _image_url_candidates(item: dict) -> list:
    """收集单张图的全部候选地址，按「无水印 → 非webp/heic → 来源优先级」排序。

    来源优先级与上游 douyin-downloader 一致（watermark_free → 原图 → 展示图 →
    常规 url_list → 下载地址 → 水印图）；同源的多个地址是不同 CDN 镜像，逐个回退。
    """
    def _urls(src) -> list:
        if isinstance(src, dict):
            src = src.get("url_list") or src.get("urlList")
        if isinstance(src, list):
            return [u for u in src if isinstance(u, str) and u]
        return []

    sources = [
        (item.get("watermark_free_download_url_list"), 0),
        (item.get("origin_image"), 1),
        (item.get("display_image"), 2),
        (item, 3),
        (item.get("download_url"), 4),
        (item.get("download_addr"), 5),
        (item.get("download_url_list"), 6),
        (item.get("owner_watermark_image"), 7),
    ]
    candidates = []
    for src, rank in sources:
        for url in _urls(src):
            low = url.lower()
            watermark = 1 if ("watermark" in low or rank >= 7) else 0
            bad_format = (1 if ".webp" in low or "format=webp" in low else 0) \
                + (1 if ".heic" in low else 0)
            candidates.append((watermark, bad_format, rank, url))
    candidates.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in candidates]


def _looks_like_image(path: Path) -> bool:
    return bool(_detect_image_ext(path))


def _detect_image_ext(path: Path) -> str:
    """按魔数识别图片格式，返回规范扩展名（jpg/png/gif/webp），非图片返回空串。"""
    if not path.exists() or path.stat().st_size < 1000:
        return ""
    head = path.read_bytes()[:16]
    if head.startswith(b"\xff\xd8"):                    # JPEG
        return "jpg"
    if head.startswith(b"\x89PNG"):                     # PNG
        return "png"
    if head.startswith((b"GIF87a", b"GIF89a")):         # GIF
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return ""


# 图文验收阈值：真实照片不会小于这个体积；抖音风控时常下发 ~1KB 占位图
_MIN_IMAGE_BYTES = 5 * 1024


def _webp_to_jpg(path: Path) -> Path | None:
    """WEBP 转 JPG（抖音 CDN 常直接下发 webp，与保存的 .jpg 后名保持一致）。"""
    try:
        ffmpeg = _resolve_ffmpeg()
    except RuntimeError:
        return None
    out = path.with_suffix(".jpg")
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(path), "-q:v", "2", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 0 and out.exists() and out.stat().st_size > 1000:
        path.unlink(missing_ok=True)
        return out
    out.unlink(missing_ok=True)
    return None


def _download_gallery(detail: dict, workdir: Path, cookie: str, progress=None) -> list[Path]:
    """下载图文作品的全部图片，按「标题_序号.扩展名」命名，返回文件路径列表。"""
    items = _iter_gallery_items(detail)
    title = _safe_filename((detail.get("desc") or "").strip() or "douyin_gallery")
    headers = ["-H", f"User-Agent: {UA}", "-H", "Referer: https://www.douyin.com/"]
    if cookie:
        headers += ["-H", f"Cookie: {cookie}"]

    saved: list[Path] = []
    total = len([it for it in items if isinstance(it, dict)])
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        if progress:
            progress(percent=min(99, int(i * 100 / total)),
                     message=f"下载图片 {i} / {total}")
        tmp = workdir / f"_douyin_img_{i}"
        ok = False
        for url in _image_url_candidates(item):
            try:
                _curl(headers + ["-o", str(tmp), url])
            except RuntimeError:
                continue
            ext = _detect_image_ext(tmp)
            if ext and tmp.stat().st_size >= _MIN_IMAGE_BYTES:
                ok = True
                break
            tmp.unlink(missing_ok=True)
        if not ok:
            for p in saved:
                p.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"图文第 {i}/{len(items)} 张图片下载失败（所有地址均不可用或只返回占位图）。"
                "常见原因：触发风控或登录态失效——请重新扫码登录后重试")
        # 统一为 .jpg：webp 转码；其余按真实格式命名（gif/png 保持原样）
        if ext == "webp":
            converted = _webp_to_jpg(tmp)
            tmp = converted or tmp
        ext = _detect_image_ext(tmp) or "jpg"
        final = workdir / f"{title}_{i}.{ext}"
        tmp.replace(final)
        saved.append(final)
    if not saved:
        raise RuntimeError("未从作品中解析出任何图片")
    return saved


def _get_music_url(detail: dict) -> str:
    """取图文作品的背景音乐地址（有 BGM 时）。"""
    music = detail.get("music") or {}
    urls = ((music.get("play_url") or {}).get("url_list")
            or (music.get("download_addr") or {}).get("url_list") or [])
    return urls[0] if urls else ""


def _safe_filename(name: str, max_len: int = 60) -> str:
    """文件名净化：去掉路径分隔符与 Windows 非法字符。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._")
    return (cleaned or "douyin_video")[:max_len]


def download_aweme(aweme_id: str, workdir: Path, cookie: str = "", mode: str = "video",
                   audio_fmt: str = "mp3", quality: str = "original", progress=None) -> tuple:
    """按作品类型下载，返回 (文件路径列表, 作品类型 kind)，kind 为 "video" / "gallery"。

    - mode="video"：视频作品 → 无水印 mp4；图文作品 → 全部图片（标题_序号.jpg）
    - mode="audio"：视频作品 → 抽取音轨；图文作品 → 提取背景音乐
    - quality（仅视频有效，音频与画质无关）：
        "original"  先探测上传原片，确认真比所选转码档大才采用，失败自动
                    退回最高转码档（默认，对齐 CLI 版 original 档语义）
        "highest"   最高转码档（不发探测请求）
        "1080p"/"720p"/…  按画面短边最接近的目标档，匹配不到自动降级
    - progress：可选进度回调 progress(percent=0-100, message=str)，由壳注入，
      供前端进度弹窗轮询展示；None 时静默（命令行直跑也能用）。

    Cookie 持久化逻辑（用户需求：填了能下就存本地，失效了再提示）：
      - 用户显式填了 cookie 且本次下载成功 → 写入 cookies.txt，下次无需重复填写；
      - 本次实际用的是「保存的」cookies.txt、却因登录态失效/风控失败 →
        清除失效的 cookies.txt 并抛出清晰提示，请用户重新粘贴；
      - 用户显式填的 cookie 本身失败 → 不碰 cookies.txt（保留里面可能还有效的旧 Cookie）；
      - 游客态（没用任何 Cookie）失败 → 沿用原始报错，提醒他「填 Cookie 能下更多」。
    """
    explicit = (cookie or "").strip()
    stored = _load_cookie(explicit="")        # 仅读文件，不受显式覆盖影响
    used = explicit or stored
    used_source = "explicit" if explicit else ("stored" if stored else "guest")

    def _mark_ok():
        if explicit:
            _save_cookie(explicit)

    try:
        if progress:
            progress(percent=2, message="解析作品信息…")
        detail = _signed_detail(aweme_id, cookie=used)

        # ---------- 图文作品 ----------
        if _iter_gallery_items(detail):
            if mode == "audio":
                bgm_url = _get_music_url(detail)
                if not bgm_url:
                    raise RuntimeError("该图文作品没有背景音乐，无法提取音频")
                headers = ["-H", f"User-Agent: {UA}", "-H", "Referer: https://www.douyin.com/"]
                if used:
                    headers += ["-H", f"Cookie: {used}"]
                title = _safe_filename((detail.get("desc") or "").strip() or f"douyin_{aweme_id}")
                tmp = workdir / f"_bgm_{aweme_id}"
                _curl(headers + ["-o", str(tmp), bgm_url])
                try:
                    audio = extract_audio(tmp, workdir, audio_fmt)
                finally:
                    tmp.unlink(missing_ok=True)
                final = workdir / f"{title}_BGM.{audio_fmt}"
                audio.replace(final)
                _mark_ok()
                return [final], "gallery"
            paths = _download_gallery(detail, workdir, used, progress=progress)
            _mark_ok()
            return paths, "gallery"

        # ---------- 视频作品 ----------
        title = _safe_filename((detail.get("desc") or "").strip() or f"douyin_{aweme_id}")
        target = workdir / f"{title}.mp4"
        headers = [
            "-H", f"User-Agent: {UA}",
            "-H", "Referer: https://www.douyin.com/",
        ]
        if used:
            headers += ["-H", f"Cookie: {used}"]
        uri, selected_size = _pick_play_addr(detail, quality)

        # 画质策略：quality=original 时先探测「上传原片」，确认其确实比所选
        # 转码档大才采用；探测失败或原片不更大 → 沿用 _pick_play_addr 选出的
        # 转码档。付费作品跳过探测（见 _is_paid_content 注释）。
        src_url = None
        expected_size = selected_size      # 进度条用的预期总大小
        if mode == "video" and quality == "original" and not _is_paid_content(detail):
            if progress:
                progress(percent=8, message="探测上传原片…")
            video_meta = detail.get("video") or {}
            probed = _probe_original(
                uri, headers,
                need_set_cookie=bool(video_meta.get("is_need_set_cookie")),
            )
            if probed and (not selected_size or probed[1] > selected_size):
                src_url = probed[0]
                expected_size = probed[1]  # 探测时已拿到原片真实大小，进度条可用
        try:
            _download_file(src_url or PLAY_URL.format(uri=uri), headers, target,
                           expected_size=expected_size, progress=progress,
                           label="下载视频")
        except RuntimeError:
            if not src_url:
                raise
            # 原画直连下载失败（CDN 限速、直链过期等）→ 退回转码档端点重下
            _download_file(PLAY_URL.format(uri=uri), headers, target,
                           expected_size=selected_size, progress=progress,
                           label="下载视频（转码档回退）")
        _assert_is_video(target)
        if mode == "audio":
            audio = extract_audio(target, workdir, audio_fmt)
            target.unlink(missing_ok=True)
            _mark_ok()
            return [audio], "video"
        _mark_ok()
        return [target], "video"
    except DouyinAuthError as exc:
        if used_source == "stored":
            # 真正生效的是本地保存的 Cookie，且它这次不行了 → 清掉并提醒重填
            _invalidate_cookie()
            raise DouyinAuthError(
                "你保存的抖音 Cookie 已失效（登录态过期或被风控拦截），下载失败。\n"
                "请重新「扫码登录抖音」或在 Cookie 框重新粘贴后重试；"
                "本次已自动清除失效的本地 Cookie。"
            ) from exc
        if used_source == "explicit":
            # 用户这回填的 Cookie 不行：别动 cookies.txt 里可能还有效的旧 Cookie
            raise DouyinAuthError(
                "你填写的抖音 Cookie 校验未通过（登录态失效或被风控拦截），下载失败。\n"
                "建议改用「扫码登录抖音」或重新复制最新 Cookie 后重试。"
            ) from exc
        # 游客态失败：沿用原始报错（已提示需填 Cookie）
        raise


def _assert_is_video(target: Path):
    """下载完成后校验文件确实是视频流，而不是抖音返回的错误页（JSON/HTML）。

    利用 MP4 容器的二进制魔数验证：真正的 MP4 文件在偏移 4 处必有 "ftyp" 字段。
    抖音错误页（JSON/HTML）开头是文本字符，不会包含 ftyp。
    比之前的文本模式检查更可靠（不受 BOM、编码等影响）。
    """
    if not target.exists():
        raise RuntimeError("视频文件未生成（下载步骤异常）")
    size = target.stat().st_size
    if size < 10_000:
        target.unlink(missing_ok=True)
        raise DouyinAuthError(
            f"视频下载不完整（仅 {size} 字节）。多半是抖音登录态失效或触发风控——"
            "请重新扫码登录或粘贴最新 Cookie 后再试。"
        )
    # 检查 MP4 魔数：偏移 4 处应为 "ftyp"
    head = target.read_bytes()[:16]
    if head[4:8] != b"ftyp":
        target.unlink(missing_ok=True)
        raise DouyinAuthError(
            "下载返回的不是视频流，而是错误页（JSON/HTML）。通常是登录态失效或触发风控——"
            "请重新扫码登录或粘贴最新 Cookie 后再试。"
        )


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
