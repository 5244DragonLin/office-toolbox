# -*- coding: utf-8 -*-
"""豆瓣书籍元数据抓取：给一个豆瓣书籍页链接，取回书名/作者/出版社/ISBN/简介/出版日期。

为什么走网页版而不是 Rexxar API：
  - 网页版详情页的 #info 区块同时给出出版社、出版年与 **ISBN**；
  - Rexxar 接口(.../api/v2/book/{id}) 虽有更完整的结构化字段（简介/目录/封面），
    但**不含 ISBN**——本功能要 ISBN，所以以网页版为准，只用标准库解析。

反爬约定（实测，别删）：
  - 页面：必须带 User-Agent，否则 418。
  - 图片：必须带 User-Agent + Referer，否则 418。
  - 默认 UA（Python-urllib）无论哪条通道都是 418。

安全：只允许豆瓣自己的域名（页面 book.douban.com / 图片 *.doubanio.com），
拒绝其它主机，避免这个接口被当成任意 URL 的代理（SSRF）。
"""
import base64
import html as _html
import re
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20

PAGE_HOST = "book.douban.com"
PAGE_PATH_RE = re.compile(r"^/subject/(\d+)/?$")
IMG_HOST_RE = re.compile(r"^(?:img\d*|img)\.doubanio\.com$|(^|\.)doubanio\.com$")

# 封面图 CDN 主机轮转列表。
# 为什么需要：豆瓣把同一张封面图分到 img1~img9 某一台上，但部分主机（实测 img9）
# 对脚本请求返回一段约 989 字节的**反爬 JS 挑战页**（Content-Type text/html，看着像 200 OK），
# 同一路径换到 img2 / img3 就能拿到真正的 JPEG。所以一台不行就换一台，
# 而不是把「取不到图」当成用户没填封面。
CDN_HOSTS = ["img3", "img2", "img1", "img4", "img5", "img6", "img7", "img8", "img9"]
SIZE_PREF = ["l", "m", "s"]
_COVER_PATH_RE = re.compile(r"^/view/subject/([a-z]+)/public/(.+)$")
MAX_COVER_ATTEMPTS = 14


class DoubanError(Exception):
    """抓取/解析失败，消息面向用户，可直接展示。"""


def _fetch(url: str, referer: str | None = None) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _text(fragment: str) -> str:
    """把一段 HTML 片段拍平成纯文本（去标签、解实体、压空白）。"""
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</(p|div|li)>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = _html.unescape(fragment)
    fragment = fragment.replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", fragment).strip()


def _subject_id(url_or_id: str) -> str:
    """从豆瓣链接（或裸 ID）里取出书籍 ID。"""
    raw = (url_or_id or "").strip()
    if not raw:
        raise DoubanError("请填写豆瓣书籍链接")
    if re.fullmatch(r"\d{4,}", raw):
        return raw
    if "://" not in raw:            # 允许不带协议直接粘 book.douban.com/...
        raw = "https://" + raw
    parts = urllib.parse.urlparse(raw)
    host = (parts.hostname or "").lower()
    if host not in (PAGE_HOST, "m.douban.com") and not host.endswith(".douban.com"):
        raise DoubanError("只支持豆瓣书籍链接（book.douban.com/subject/…）")
    m = PAGE_PATH_RE.match(parts.path)
    if not m:
        raise DoubanError("链接格式不对，应形如 https://book.douban.com/subject/35114602/")
    return m.group(1)


def _parse_info(info_html: str) -> dict[str, str]:
    """解析 #info 区块：按 <span class="pl">标签</span> 切段取值。

    结构形如：
        <span class="pl"> 出版社</span>
          <a href="…">中信出版集团</a>
        <br>
    所以「标签」到「下一个标签」之间的内容就是该字段的值。
    """
    out: dict[str, str] = {}
    marks = list(re.finditer(r'<span class="pl">\s*([^<:：]+?)\s*[:：]?\s*</span>', info_html))
    for i, m in enumerate(marks):
        label = m.group(1).strip()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(info_html)
        value = _text(info_html[m.end():end])
        value = re.sub(r"^[:：]\s*", "", value).strip()
        if label and value and label not in out:
            out[label] = value
    return out


def _parse_description(html_text: str) -> str:
    """取内容简介。

    豆瓣改版后结构为 <div class="indent" id="link-report">，完整简介在
    <span class="all hidden"> 里（简短版带「展开全部」按钮）；老结构直接是
    <div class="intro">。优先要完整版。
    """
    i = html_text.find('id="link-report"')
    scope = html_text[i:i + 20000] if i >= 0 else html_text
    m = re.search(r'<span class="all[^"]*"[^>]*>(.*?)</span>\s*</div>', scope, re.S)
    if not m:
        m = re.search(r'<div class="intro"[^>]*>(.*?)</div>', scope, re.S)
        if not m:
            # 兜底：整页找第一个 intro
            m = re.search(r'<div class="intro"[^>]*>(.*?)</div>', html_text, re.S)
    if not m:
        return ""
    block = re.sub(r"<style.*?</style>", "", m.group(1), flags=re.S)
    paragraphs = [_text(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", block, re.S)]
    paragraphs = [p for p in paragraphs if p and not re.fullmatch(r"[\s·…\.]+", p)]
    if paragraphs:
        return "\n".join(paragraphs)
    return _text(block)


def _parse_cover(html_text: str) -> str:
    """取封面图链接（优先大图）。"""
    m = re.search(r'<div id="mainpic".*?<img[^>]+src="([^"]+)"', html_text, re.S)
    if not m:
        return ""
    url = m.group(1)
    # 页面给的是 /s/（小图），换成 /l/（大图）更清晰；换不成还有原图兜底
    return re.sub(r"/view/subject/[a-z]/public/", "/view/subject/l/public/", url)


def fetch_book_meta(url_or_id: str, with_cover: bool = True) -> dict:
    """抓取并解析豆瓣书籍元数据。

    返回：{"id", "url", "title", "author", "publisher", "isbn", "date",
          "description", "cover": {"dataUrl","name"} | None}
    """
    bid = _subject_id(url_or_id)
    page_url = f"https://{PAGE_HOST}/subject/{bid}/"
    try:
        raw = _fetch(page_url, referer=f"https://{PAGE_HOST}/")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise DoubanError(f"豆瓣上没有这本书（{bid}），请检查链接") from exc
        raise DoubanError(f"豆瓣返回 {exc.code}，稍后再试") from exc
    except Exception as exc:  # noqa: BLE001
        raise DoubanError(f"访问豆瓣失败：{exc}") from exc

    html_text = raw.decode("utf-8", errors="replace")

    mt = re.search(r'<span property="v:itemreviewed">\s*(.*?)\s*</span>', html_text, re.S)
    title = _text(mt.group(1)) if mt else ""
    mi = re.search(r'<div id="info"[^>]*>(.*?)</div>', html_text, re.S)
    info = _parse_info(mi.group(1)) if mi else {}

    book = {
        "id": bid,
        "url": page_url,
        "title": title,
        "author": info.get("作者", ""),
        "publisher": info.get("出版社", ""),
        "isbn": re.sub(r"[^0-9Xx]", "", info.get("ISBN", "")),
        "date": info.get("出版年", ""),
        "description": _parse_description(html_text),
        "translator": info.get("译者", ""),
        "pages": info.get("页数", ""),
        "price": info.get("定价", ""),
        "binding": info.get("装帧", ""),
        "subtitle": info.get("副标题", ""),
        "origin_title": info.get("原作名", ""),
        "cover": None,
        "cover_failed": False,
    }
    if not book["title"] and not book["author"]:
        raise DoubanError("页面解析失败，豆瓣可能改了版式（也可能是验证码页），请反馈")

    if with_cover:
        page_cover = _parse_cover(html_text)
        book["cover"] = _fetch_cover(book_id=bid, cover_url=page_cover)
        # 页面有封面图却全部下载失败（如所有 CDN 主机都返反爬页）：
        # 明确告知前端，让用户知道是「没抓到」而不是「这本书没封面」
        book["cover_failed"] = bool(page_cover) and not book["cover"]
    return book


def _cover_candidates(cover_url: str) -> list[str]:
    """给定页面里的封面图 URL，生成候选列表：先原样试，再换 CDN 主机 / 尺寸重试。

    路径形如 /view/subject/l/public/s35620174.jpg，其中主机可换、尺寸段可换，
    图片 id（s35620174）固定。去重且保序，原 URL 永远排第一。
    """
    out = [cover_url]
    parsed = urllib.parse.urlparse(cover_url)
    m = _COVER_PATH_RE.match(parsed.path)
    if not m:
        return out
    cur_size, name = m.group(1), m.group(2)
    sizes = [cur_size] + [s for s in SIZE_PREF if s != cur_size]
    hosts = [parsed.hostname or ""] + [h + ".doubanio.com" for h in CDN_HOSTS]
    for host in hosts:
        if not host:
            continue
        for size in sizes:
            out.append(f"https://{host}/view/subject/{size}/public/{name}")
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:MAX_COVER_ATTEMPTS]


def _fetch_cover(book_id: str, cover_url: str) -> dict | None:
    """下载封面并转成 dataURL（前端可直接还原成 File 上传，无需服务端临时文件）。

    换 CDN 主机重试，只有真图片（魔数校验通过）才算数；反爬挑战页会被丢弃。
    """
    if not cover_url:
        return None
    referer = f"https://{PAGE_HOST}/subject/{book_id}/"
    for candidate in _cover_candidates(cover_url):
        host = (urllib.parse.urlparse(candidate).hostname or "").lower()
        if not IMG_HOST_RE.search(host):
            continue
        try:
            blob = _fetch(candidate, referer=referer)
        except Exception:  # noqa: BLE001
            continue
        if not blob or len(blob) < 128:
            continue
        if blob[:3] == b"\xff\xd8\xff":
            mime, ext = "image/jpeg", ".jpg"
        elif blob[:8] == b"\x89PNG\r\n\x1a\n":
            mime, ext = "image/png", ".png"
        elif blob[:6] in (b"GIF87a", b"GIF89a"):
            mime, ext = "image/gif", ".gif"
        elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            mime, ext = "image/webp", ".webp"
        else:
            continue   # 反爬挑战页 / 其它非图片响应，换下一个候选
        return {
            "dataUrl": f"data:{mime};base64," + base64.b64encode(blob).decode("ascii"),
            "name": f"cover{ext}",
            "bytes": len(blob),
        }
    return None
