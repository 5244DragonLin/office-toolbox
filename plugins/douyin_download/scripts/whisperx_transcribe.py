"""WhisperX 语音转文字封装（抖音插件专用，纯文字输出）。

底层复用开源 ASR 引擎 faster-whisper，本文件只做编排：
  - faster-whisper  —— 听声音、写字（开源、可离线）

设计约定（对齐 office-toolbox 壳）：
  - 纯函数，可被 plugin.py 直接调用，不依赖壳。
  - 进度通过 progress(percent, message) 回调上报；传 None 时静默（命令行也能用）。
  - 输出文件写入 out_dir，返回 [路径, ...]；音频只是「过路」临时文件，不返回。
  - ASR 模型优先从 ModelScope（魔搭，国内快）下载并缓存到本地；
    若 ModelScope 不可用则回退到 Hugging Face（默认走 hf-mirror.com 国内镜像）。之后离线使用。

为什么先把音频转成 16k 单声道 wav 再喂给 whisperx：
  whisperx.load_audio 内部走 torchaudio，而 torchaudio 读 mp3/flac 需要 ffmpeg 后端，
  本机 ffmpeg 由 imageio-ffmpeg 提供（一个独立二进制，不一定注册给 torchaudio）。
  先转成 wav，torchaudio 走 soundfile（libsndfile）直接读，彻底绕开 ffmpeg 依赖坑。
"""
from __future__ import annotations

import os

# 兜底镜像：当 ModelScope 不可用时，回退到 Hugging Face 公益镜像（hf-mirror.com），
# 加速首次模型下载。若用户已自行设置 HF_ENDPOINT（如走代理或海外官方源），此处不覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import subprocess
import threading
from pathlib import Path

import whisperx
import torch
from imageio_ffmpeg import get_ffmpeg_exe


# ---------------------------------------------------------------------------
# ModelScope 仓库映射（faster-whisper 官方 Systran 组织，CTranslate2 格式）
# ---------------------------------------------------------------------------
_MS_REPO = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large": "Systran/faster-whisper-large-v3",
    "large-v3": "Systran/faster-whisper-large-v3",
}

_MODEL_CACHE = Path.home() / ".cache" / "office-toolbox" / "models"


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f}GB"
    return f"{n / 1024 ** 2:.1f}MB"


def _make_modelscope_callbacks(model_size: str, progress):
    """构造 modelscope snapshot_download 的 progress_callbacks（回调类列表）。

    modelscope 对每个文件实例化一次回调类（cls(filename, file_size)）并在多个
    下载线程里调 update(size)，这里用闭包共享状态把逐文件字节量聚合成整体
    进度，映射到任务进度的 10%~35% 区间。
    """
    state = {"total": 0, "done": 0}
    lock = threading.Lock()

    class _ModelScopeProgress:
        def __init__(self, filename: str, file_size: int):
            self._size = max(0, int(file_size or 0))
            with lock:
                state["total"] += self._size

        def update(self, size: int) -> None:
            self._bump(size)

        def end(self) -> None:
            pass  # 正常结束时 update 已累计到 file_size

        def _bump(self, n: int):
            with lock:
                state["done"] += int(n)
                done, total = state["done"], state["total"]
            if not progress:
                return
            if total > 0:
                pct = 10 + int(min(done / total, 1.0) * 25)
                progress(percent=pct, message=(
                    f"从 ModelScope 下载 ASR 模型（{model_size}）："
                    f"{_fmt_bytes(done)} / {_fmt_bytes(total)}"))
            else:
                # 服务端没给文件大小时无法算百分比，至少让用户看到字节在涨
                progress(message=f"从 ModelScope 下载 ASR 模型（{model_size}）：已下载 {_fmt_bytes(done)}")

    return [_ModelScopeProgress]


def _resolve_asr_model(model_size: str, progress=None) -> list[str]:
    """返回喂给 whisperx.load_model 的候选引用列表（按优先级）：
    [本地目录路径（ModelScope 已下载）, 尺寸名（回退 WhisperX 原生 HF 下载）]。"""
    candidates = []
    repo = _MS_REPO.get(model_size)
    if repo:
        local_dir = _MODEL_CACHE / f"faster-whisper-{model_size}"
        if (local_dir / "config.json").exists():
            candidates.append(str(local_dir))
        else:
            try:
                if progress:
                    progress(percent=10, message=f"从 ModelScope 下载 ASR 模型（{model_size}）…首次较慢，请稍候")
                from modelscope import snapshot_download
                # 新版 modelscope 支持逐文件进度回调，做实时下载进度；旧版静默降级
                kwargs = {}
                try:
                    import inspect as _inspect
                    if "progress_callbacks" in _inspect.signature(snapshot_download).parameters:
                        kwargs["progress_callbacks"] = _make_modelscope_callbacks(model_size, progress)
                except (TypeError, ValueError):
                    pass
                snapshot_download(model_id=repo, local_dir=str(local_dir), **kwargs)
                candidates.append(str(local_dir))
            except Exception as exc:  # noqa: BLE001 — 魔搭不可用则回退 HF
                if progress:
                    progress(message=f"ModelScope 下载失败，回退 Hugging Face（{exc}）")
    candidates.append(model_size)  # 最终兜底：WhisperX 原生（HF / hf-mirror）
    return candidates


# ---------------------------------------------------------------------------
# 音频预处理：转成 16k 单声道 PCM wav
# ---------------------------------------------------------------------------
def _to_16k_wav(src: Path, dst: Path) -> Path:
    """用 imageio-ffmpeg 提供的静态 ffmpeg 把任意音视频转成 whisperx 最稳的 wav。"""
    ffmpeg = get_ffmpeg_exe()
    cmd = [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-f", "wav", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not dst.exists():
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise RuntimeError("ffmpeg 音频预处理失败：" + " | ".join(tail))
    return dst


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def transcribe_audio(
    audio_path,
    out_dir,
    *,
    model_size: str = "small",
    language: str | None = None,
    device: str | None = None,
    compute_type: str | None = None,
    progress=None,
) -> list[str]:
    """把一段音视频转写成纯文字 .txt，写入 out_dir，返回 [txt 路径]。

    参数：
      audio_path   : 原始音视频路径（任意 ffmpeg 能读的格式）
      out_dir      : 输出目录（通常是壳的临时 workdir）
      model_size   : tiny/base/small/medium/large，越大越准越慢
      language     : 语言代码（zh/en…），None 则自动识别
    """
    audio_path = Path(audio_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 设备与精度：有显卡用 float16 又快又省；纯 CPU 用 int8 量化
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if compute_type is None:
        compute_type = "float16" if device == "cuda" else "int8"

    # 1) 音频预处理 → 16k 单声道 wav（绕开 torchaudio 的 ffmpeg 后端坑）
    if progress:
        progress(percent=8, message="预处理音频（转 16k 单声道）…")
    wav_path = out_dir / f"{audio_path.stem}.16k.wav"
    _to_16k_wav(audio_path, wav_path)
    audio = whisperx.load_audio(str(wav_path))

    # 2) 解析 ASR 模型候选（ModelScope 本地优先，失败回退 HF 名称）
    candidates = _resolve_asr_model(model_size, progress)

    # 3) 加载并转写（本地路径失败则自动回退下一个候选）
    if progress:
        progress(percent=40, message=f"加载 WhisperX 模型（{model_size}）…")
    model = None
    last_err = None
    for ref in candidates:
        try:
            is_local = Path(ref).is_dir()
            if progress and not is_local:
                # 走到这里说明 ModelScope 未命中，将从 HF（hf-mirror）联网下载
                progress(percent=40, message=f"从 Hugging Face 下载模型（{ref}）…首次较慢，请耐心等待")
            model = whisperx.load_model(
                ref, device,
                compute_type=compute_type,
                language=language or None,
                local_files_only=is_local,
            )
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if progress:
                progress(message=f"模型加载失败（{ref}），尝试下一个来源…")
    if model is None:
        raise RuntimeError(f"WhisperX 模型加载失败：{last_err}")

    batch_size = 16 if device == "cuda" else 4
    if progress:
        progress(percent=55, message="语音转写中…")
    # vad_filter=False：关掉 WhisperX 默认 VAD 预处理，该预处理依赖 pyannote/segmentation
    # 门控模型（需 HF Token），与「零 Token」目标冲突；关闭仅损失长音频分段提速，对
    # 单人口播转文字质量几乎无影响。
    result = model.transcribe(audio, batch_size=batch_size, vad_filter=False)
    segments = result.get("segments", [])

    # 4) 写出纯文字
    if progress:
        progress(percent=95, message="生成文字稿…")
    lines = [
        (seg.get("text") or "").strip()
        for seg in segments
        if (seg.get("text") or "").strip()
    ]
    txt_path = out_dir / f"{audio_path.stem}.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    if progress:
        progress(percent=100, message="完成")
    return [str(txt_path)]


# ---------------------------------------------------------------------------
# 命令行直跑（便于单独测试，不影响插件调用）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="WhisperX 语音转文字（纯文字）")
    ap.add_argument("file", help="音视频文件")
    ap.add_argument("--model", default="small")
    ap.add_argument("--lang", default=None, help="语言代码，留空自动识别")
    ap.add_argument("--out", default=".", help="输出目录")
    args = ap.parse_args()

    outs = transcribe_audio(
        args.file, args.out,
        model_size=args.model, language=args.lang,
    )
    print("输出文件：")
    for o in outs:
        print(" -", o)
