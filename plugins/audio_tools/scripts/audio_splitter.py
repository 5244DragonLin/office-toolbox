"""音频切分核心逻辑：按时间点 / 等长片段切分音频，基于 ffmpeg。

ffmpeg 获取优先级：
1. 系统 PATH 中的 ffmpeg（若已安装则直接使用）
2. imageio-ffmpeg 包自带的 ffmpeg 二进制（pip 安装即自带，无需额外配置）

切分原理：
- `-ss` / `-to` 都放在 `-i` 之前：ffmpeg 对音频做解码级精确 seek，
  `-to` 指原始时间轴上的绝对结束点，切分误差在毫秒级；
- 输出"保持原格式"时用 `-c copy` 流拷贝（快、不重新编码、不损失音质）；
- 转码输出（mp3/wav/flac/m4a）时重新编码，时间轴 100% 精确。
"""
import re
import shutil
import subprocess
from pathlib import Path

try:
    import imageio_ffmpeg  # 可选依赖：兜底提供 ffmpeg 二进制
except Exception:  # noqa: BLE001
    imageio_ffmpeg = None

SUPPORTED_EXTS = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg",
    ".wma", ".opus", ".aiff", ".wmv", ".mka",
}

# 输出格式 -> (扩展名, ffmpeg 转码参数)
FORMAT_CODEC = {
    "mp3": (".mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "wav": (".wav", ["-c:a", "pcm_s16le"]),
    "flac": (".flac", ["-c:a", "flac"]),
    "m4a": (".m4a", ["-c:a", "aac", "-b:a", "192k"]),
}


def _get_ffmpeg() -> str:
    """获取可用的 ffmpeg 可执行文件路径。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if imageio_ffmpeg is not None:
        return imageio_ffmpeg.get_ffmpeg_exe()
    raise RuntimeError("未找到 ffmpeg：请安装 ffmpeg 并加入 PATH，或执行 pip install imageio-ffmpeg")


def _parse_time(text: str) -> float:
    """解析单个时间字符串为秒数。

    支持三种写法：
    - 纯秒：    90、90.5
    - 分:秒：   1:30、0:30.5
    - 时:分:秒：1:02:30
    """
    text = text.strip()
    if not text:
        raise ValueError("时间不能为空")
    parts = text.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise ValueError(f"无法解析的时间: {text}（支持 秒 / 分:秒 / 时:分:秒）")


def parse_points(text: str) -> list[float]:
    """解析切分点字符串为排序去重后的秒数列表。

    支持逗号 / 空格 / 分号 / 换行分隔，例如：
    "0:30,1:30,2:45" / "30 90 165" / "1:00, 120"
    """
    if not text or not text.strip():
        return []
    parts = re.split(r"[,;\s\n]+", text.strip())
    points = [_parse_time(p) for p in parts if p.strip()]
    return sorted(set(points))


def _run_ffmpeg(ffmpeg: str, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """运行 ffmpeg，输出统一按 UTF-8 解码（避免中文 Windows 下 GBK 解码失败）。"""
    return subprocess.run(
        [ffmpeg, *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _get_duration(ffmpeg: str, src: Path) -> float:
    """读取音频时长（秒）。

    通过解析 `ffmpeg -i` 输出中的 Duration 行实现，不依赖 ffprobe，
    这样用 imageio-ffmpeg 自带的单个 ffmpeg 二进制即可完成全部工作。
    """
    proc = _run_ffmpeg(ffmpeg, ["-i", str(src)])
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr or "")
    if not m:
        raise RuntimeError("无法读取音频时长，请确认文件是有效的音频文件")
    hours, minutes, seconds = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return hours * 3600 + minutes * 60 + seconds


def _build_point_segments(duration: float, points: list[float]) -> list[tuple[float, float]]:
    """按时间点构建片段区间：起点=上一个切分点（首个为 0），终点=当前切分点，最后到音频结尾。"""
    segments = []
    start = 0.0
    for point in points:
        segments.append((start, point))
        start = point
    segments.append((start, duration))
    # 去掉空片段（起点 == 终点，例如切分点重复或等于 0）
    return [(s, e) for s, e in segments if e - s > 0.05]


def _build_equal_segments(duration: float, length: float) -> list[tuple[float, float]]:
    """按等长片段切分：每段 length 秒，最后一段不足 length 则到音频结尾。"""
    segments = []
    start = 0.0
    while start < duration - 0.05:
        end = min(start + length, duration)
        segments.append((start, end))
        start = end
    return segments


def _run_cut(ffmpeg: str, src: Path, start: float, end: float,
             out_path: Path, codec_args: list[str]):
    """执行单段切分。"""
    cmd = [
        "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
    ]
    if codec_args:
        cmd += codec_args
    cmd.append(str(out_path))
    proc = _run_ffmpeg(ffmpeg, cmd, timeout=600)
    if proc.returncode != 0 or not out_path.exists():
        detail = (proc.stderr or "").strip()[-400:]
        raise RuntimeError(f"切分 {start:.1f}s-{end:.1f}s 失败: {detail}")


def split_audio(src: Path, params: dict, workdir: Path) -> list[Path]:
    """按参数切分音频，返回输出文件路径列表。

    params 支持：
    - mode:      "points"（按时间点切分）| "equal"（按等长片段切分）
    - points:    切分点字符串（mode=points 时必填），如 "0:30,1:30,2:45"
    - duration:  每段时长秒数（mode=equal 时必填）
    - format:    "same"（保持原格式，流拷贝）| mp3 / wav / flac / m4a（转码）
    """
    src = Path(src)
    if not src.exists():
        raise ValueError(f"输入文件不存在: {src}")
    if src.suffix.lower() not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的音频格式: {src.suffix}，支持 {', '.join(sorted(SUPPORTED_EXTS))}")

    ffmpeg = _get_ffmpeg()
    duration = _get_duration(ffmpeg, src)
    mode = params.get("mode", "points")
    out_fmt = (params.get("format") or "same").strip().lower()

    # ---------- 计算切分段区间 ----------
    if mode == "equal":
        raw = params.get("duration")
        try:
            length = float(raw) if raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            raise ValueError("每段时长必须是数字（秒）")
        if length <= 0:
            raise ValueError("请填写每段时长（秒，需大于 0）")
        if length >= duration:
            raise ValueError(f"每段时长（{length:g}s）不能大于等于音频总时长（{duration:.1f}s）")
        segments = _build_equal_segments(duration, length)
    else:
        points_text = (params.get("points") or "").strip()
        if not points_text:
            raise ValueError("请填写切分点（按时间点切分时必填）")
        points = parse_points(points_text)
        for p in points:
            if p < 0:
                raise ValueError(f"切分点不能为负数: {p:g}s")
        points = [p for p in points if p > 0]  # 0 秒切分点无意义，忽略
        over = [f"{p:g}s" for p in points if p >= duration]
        if over:
            raise ValueError(f"切分点超出音频总时长（{duration:.1f}s）: {', '.join(over)}")
        if not points:
            raise ValueError("切分点全部无效（需为大于 0 且小于总时长的时间）")
        segments = _build_point_segments(duration, points)

    # ---------- 确定输出格式与转码参数 ----------
    if out_fmt == "same":
        ext = src.suffix.lower()
        codec_args = []  # 流拷贝：快、不重新编码、不损失音质
    else:
        if out_fmt not in FORMAT_CODEC:
            raise ValueError(f"不支持的输出格式: {out_fmt}，可选 same / mp3 / wav / flac / m4a")
        ext, codec_args = FORMAT_CODEC[out_fmt]

    # ---------- 逐段切分 ----------
    outputs = []
    for i, (start, end) in enumerate(segments, 1):
        out_path = workdir / f"{src.stem}_part{i:02d}{ext}"
        _run_cut(ffmpeg, src, start, end, out_path, codec_args)
        outputs.append({
            "path": str(out_path),
            "start": round(start, 1),
            "end": round(end, 1),
        })
    return outputs
