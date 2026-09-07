"""图片处理核心逻辑：分辨率调整 / 压缩 / 增强，基于 Pillow。

设计要点：
- 纯函数：输入 (源路径, 参数, 输出目录) -> 输出信息 dict，不依赖壳的任何约定，
  可脱离 Web 服务单独测试、被其他插件复用；
- 所有动作共用统一管线：EXIF 方向转正 -> (按动作插入处理算子) -> 显式质量保存。
  EXIF 转正放在第一步：手机竖拍照片自带 Orientation 标记，先转正再处理，
  否则任何后续几何运算（缩放/放大）都会把方向做错；
- JPEG/WebP 保存显式传 quality：Pillow 的 save() 默认 quality=75，处理后重存
  会隐性降质一次——这是很多"图片工具越用越糊"的根源，必须杜绝；
- 「压完反而更大就保留原图」：本就高度压缩的小图再压只会更大，此时直接搬运
  原始字节，避免"压缩工具越压越大"。

输出约定：每个处理函数返回 {"path": 输出路径, ...附加信息}，无附加信息时不带
额外键。调用方（plugin.py）把 path 换成 str、其余键透传给壳的结果列表即可。
"""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

# 支持读入的图片格式（gif 只取首帧）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

# 缩放插值算法：LANCZOS 细节保留最好（默认），BICUBIC 次之；
# NEAREST 仅用于像素风图片（放大不产生过渡像素）
_INTERP = {
    "LANCZOS": Image.LANCZOS,
    "BICUBIC": Image.BICUBIC,
    "BILINEAR": Image.BILINEAR,
    "NEAREST": Image.NEAREST,
}

# 输出格式选项 -> (Pillow 格式名, 默认扩展名)。keep 在 _resolve_format 里解析
_FORMATS = {
    "jpg": ("JPEG", ".jpg"),
    "png": ("PNG", ".png"),
    "webp": ("WEBP", ".webp"),
}

# 扩展名 -> Pillow 格式名（keep 时按源图扩展名判断输出格式）
_EXT_FORMAT = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP",
    ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF", ".gif": "GIF",
}

# 不透明格式：透明通道需要铺白底，否则 Pillow 转 JPEG 时报错或变黑底
_OPAQUE_FORMATS = {"JPEG", "BMP"}

# 锐化强度档位 -> UnsharpMask(radius, percent, threshold) 参数。
# radius 越大作用范围越广，percent 是锐化力度，threshold 过滤弱边缘防噪点被放大
_SHARPEN = {
    "light": (1.2, 60, 3),
    "medium": (2.0, 120, 3),
    "strong": (2.5, 180, 2),
}

# 压缩/调整之外的场景（如重存转正后的图）统一使用的显式保存质量
_DEFAULT_SAVE_QUALITY = 92


# ---------- 公共骨架 ----------

def _parse_src(src) -> Image.Image:
    """打开图片并完成 EXIF 方向转正（所有动作的第一步）。"""
    img = Image.open(src)
    img.load()
    transposed = ImageOps.exif_transpose(img)
    return transposed if transposed is not None else img


def _resolve_format(src: Path, fmt_option: str) -> tuple[str, str]:
    """解析输出格式选项 -> (Pillow 格式名, 输出扩展名)。

    keep 时沿用源图格式；源图是不支持的冷门格式（如 16 位 TIFF）时回落为 PNG。
    """
    if fmt_option != "keep":
        return _FORMATS[fmt_option]
    fmt = _EXT_FORMAT.get(src.suffix.lower())
    if fmt in ("JPEG", "PNG", "WEBP"):
        return fmt, src.suffix.lower()
    return "PNG", ".png"


def _prepare_for_format(img: Image.Image, fmt: str) -> Image.Image:
    """按输出格式修正色彩模式：JPEG/BMP 铺白底，其余保留 alpha。"""
    if fmt not in _OPAQUE_FORMATS:
        return img
    if img.mode in ("RGBA", "LA", "PA"):
        base = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        base.paste(rgba, mask=rgba.split()[-1])
        return base
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")
    return img


def _save(img: Image.Image, out_path: Path, fmt: str, quality: int) -> bytes:
    """显式质量保存，返回写入的字节数。quality 仅对有损格式生效。"""
    img = _prepare_for_format(img, fmt)
    if fmt == "JPEG":
        img.save(out_path, "JPEG", quality=quality, subsampling=1)
    elif fmt == "WEBP":
        img.save(out_path, "WEBP", quality=quality)
    else:
        img.save(out_path, "PNG")
    return out_path.stat().st_size


class _BytesPath:
    """让 _save / Image.save 能写入 BytesIO 的极简路径替身（只实现 stat/write）。"""

    def __init__(self, buf: BytesIO):
        self._buf = buf
        self.size = 0

    def write(self, data: bytes):
        self._buf.write(data)
        self.size = len(data)

    def stat(self):
        class _St:
            st_size = 0
        _St.st_size = self.size
        return _St()


def _encode(img: Image.Image, fmt: str, quality: int) -> bytes:
    """把图片编码为字节（供体积比较 / 二分逼近用），不落盘。"""
    buf = BytesIO()
    _save(img, _BytesPath(buf), fmt, quality)
    return buf.getvalue()


def _resolve_out(outdir, out_name, fallback: str) -> Path:
    """确定输出路径：调用方未指定时用「原名 + 动作后缀」避免覆盖源图。"""
    if out_name:
        return Path(out_name)
    return Path(outdir) / fallback


def _target_size(orig_w: int, orig_h: int, params: dict) -> tuple[int, int]:
    """按五选一的目标方式计算新尺寸（互斥：长边/短边/百分比/宽/高）。

    五种方式分两类：
    - 长边/短边是「以图为准」的相对定位：横竖混排批量处理时每张图各自的
      长边（或短边）对齐到目标值，结果视觉尺寸均匀；
    - 宽/高是「以版面为准」的绝对定位：该轴无条件对齐目标值。
    都保持宽高比，不做拉伸。
    """
    w, h = orig_w, orig_h
    try:
        value = float(params.get("value"))
    except (TypeError, ValueError):
        raise ValueError("目标值必须是数字")
    if value <= 0:
        raise ValueError("目标值必须大于 0")

    mode = params.get("mode", "long_edge")
    if mode == "long_edge":
        if w >= h:
            w, h = value, max(1, round(h * value / w))
        else:
            w, h = max(1, round(w * value / h)), value
    elif mode == "short_edge":
        if w <= h:
            w, h = value, max(1, round(h * value / w))
        else:
            w, h = max(1, round(w * value / h)), value
    elif mode == "percent":
        scale = value / 100.0
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
    elif mode == "width":
        w, h = value, max(1, round(h * value / w))
    elif mode == "height":
        w, h = max(1, round(w * value / h)), value
    else:
        raise ValueError(f"未知的目标方式: {mode}")

    # 「只缩不放」：目标计算结果大于原图时保持原尺寸，防止小图被放大虚掉
    if str(params.get("no_upscale", "")).lower() in ("1", "true", "yes", "on") \
            and (w > orig_w or h > orig_h):
        return orig_w, orig_h
    return int(w), int(h)


def _is_yes(value) -> bool:
    """前端 select 提交的是字符串，宽松解析布尔参数。"""
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是")


# ---------- 动作一：分辨率调整 ----------

def resize_image(src, params: dict, outdir, out_name=None) -> dict:
    """调整像素尺寸。参数：mode/value/interp/no_upscale/format。

    尺寸与格式都没变时直接搬运原始字节（无损透传），不重编码。
    """
    src, outdir = Path(src), Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with _parse_src(src) as img:
        fmt, ext = _resolve_format(src, params.get("format", "keep"))
        new_size = _target_size(img.width, img.height, params)
        if new_size == img.size and _EXT_FORMAT.get(src.suffix.lower()) == fmt:
            out_path = _resolve_out(outdir, out_name, f"{src.stem}_处理{ext}")
            out_path.write_bytes(src.read_bytes())
            return {"path": out_path, "note": "尺寸与格式均未变化，直接保留原图"}
        resized = img.resize(new_size, _INTERP.get(params.get("interp", "LANCZOS"), Image.LANCZOS))
        out_path = _resolve_out(outdir, out_name, f"{src.stem}_处理{ext}")
        _save(resized, out_path, fmt, _DEFAULT_SAVE_QUALITY)
    return {"path": out_path, "size": f"{new_size[0]}x{new_size[1]}"}


# ---------- 动作二：图片压缩 ----------

def compress_image(src, params: dict, outdir, out_name=None) -> dict:
    """压缩文件体积，像素默认不动。参数：target/quality/target_kb/scale/format。

    target=quality：直接以指定质量重编码；
    target=size：以目标体积(KB)二分质量逼近——质量与体积单调相关，
    二分 7 轮即可把结果收敛到目标体积附近；
    scale 填 1-100 以外的正数时先按百分比缩放（压缩与缩放可叠加）。
    """
    src, outdir = Path(src), Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    orig_len = src.stat().st_size
    with _parse_src(src) as img:
        fmt, ext = _resolve_format(src, params.get("format", "keep"))
        scale = float(params.get("scale") or 100)
        if scale <= 0:
            raise ValueError("叠加缩放必须是正数")
        if scale != 100:
            img = img.resize((max(1, round(img.width * scale / 100)),
                              max(1, round(img.height * scale / 100))), Image.LANCZOS)

        target_kb = params.get("target_kb")
        if params.get("target") == "size" and target_kb:
            data, quality = _compress_to_size(img, fmt, float(target_kb))
        else:
            quality = int(params.get("quality") or 75)
            if not 1 <= quality <= 100:
                raise ValueError("质量必须在 1-100 之间")
            data = _encode(img, fmt, quality)

        # 保护规则：没缩放、没转格式、按质量压缩后反而更大 → 保留原图字节
        note = None
        if scale == 100 and _EXT_FORMAT.get(src.suffix.lower()) == fmt \
                and params.get("target") != "size" and len(data) >= orig_len:
            data, note = src.read_bytes(), "压缩后体积未减小，已保留原图"

        out_path = _resolve_out(outdir, out_name, f"{src.stem}_压缩{ext}")
        out_path.write_bytes(data)
    result = {"path": out_path,
              "ratio": f"{orig_len / 1024:.0f}KB -> {len(data) / 1024:.0f}KB"}
    if note:
        result["note"] = note
    return result


def _compress_to_size(img: Image.Image, fmt: str, target_kb: float) -> tuple[bytes, int]:
    """二分质量逼近目标体积，返回 (已编码字节, 最终质量)。

    质量与体积单调相关，二分 7 轮足够把质量档收敛到个位数精度。
    连最低质量都压不进目标时，返回最低质量的尽力而为结果。
    """
    if fmt == "PNG":
        raise ValueError("PNG 是无损格式，无法按目标体积压缩；请改用 JPG/WebP 输出")
    target = target_kb * 1024
    lo, hi = 5, 95
    best, best_q = None, lo
    while lo <= hi:
        mid = (lo + hi) // 2
        data = _encode(img, fmt, mid)
        if len(data) <= target:
            best, best_q = data, mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        best, best_q = _encode(img, fmt, 5), 5
    return best, best_q


# ---------- 动作三：图片增强 ----------

def enhance_image(src, params: dict, outdir, out_name=None) -> dict:
    """观感质量增强。参数：upscale/sharpen/autocontrast/denoise/format。

    管线顺序固定：降噪 -> 2倍放大(LANCZOS) -> 放大后锐化 -> 用户锐化 -> 自动对比度。
    放大在锐化之前：插值放大会让边缘变软，先放大再锐化才能把细节收回来，
    这也是本动作与"纯 resize"在观感上的本质差距。
    """
    src, outdir = Path(src), Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with _parse_src(src) as img:
        steps = []
        if _is_yes(params.get("denoise")):
            img = img.filter(ImageFilter.MedianFilter(3))
            steps.append("降噪")
        if _is_yes(params.get("upscale")):
            img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
            img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=90, threshold=2))
            steps.append("2倍放大")
        sharpen = params.get("sharpen", "medium")
        if sharpen and sharpen != "none":
            if sharpen not in _SHARPEN:
                raise ValueError(f"未知锐化档位: {sharpen}")
            img = img.filter(ImageFilter.UnsharpMask(*_SHARPEN[sharpen]))
            steps.append("锐化")
        if _is_yes(params.get("autocontrast")):
            img = ImageOps.autocontrast(img, cutoff=1)
            steps.append("自动对比度")
        if not steps:
            raise ValueError("至少选择一项增强操作（放大/锐化/对比度/降噪）")
        fmt, ext = _resolve_format(src, params.get("format", "keep"))
        out_path = _resolve_out(outdir, out_name, f"{src.stem}_增强{ext}")
        _save(img, out_path, fmt, _DEFAULT_SAVE_QUALITY)
    return {"path": out_path, "ops": "+".join(steps)}
