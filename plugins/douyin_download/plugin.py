"""抖音下载插件：将 scripts/ 下的 douyin_fetch.py 包装为统一插件动作。"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from douyin_fetch import extract_audio, extract_aweme_id, download_aweme  # noqa: E402
from douyin_login import login_start, login_poll, login_logout  # noqa: E402
from whisperx_transcribe import transcribe_audio  # noqa: E402

# 扫码登录契约：壳的 /api/login/{pid}/start|poll 路由会调用这两个函数，
# 前端弹窗展示二维码并轮询状态；logout 仍走普通动作返回提示文件。
LOGIN_PROVIDER = {
    "start": login_start,
    "poll": login_poll,
}


def _require_link(params: dict) -> str:
    link = (params.get("link") or "").strip()
    if not link:
        raise ValueError("请填写抖音链接或视频 ID（必填项）")
    return link


def act_login_logout(files, params, workdir: Path):
    """清除已保存的抖音登录凭证。"""
    return [login_logout(workdir)]


def act_download_video(files, params, workdir: Path):
    """按链接/ID 下载：视频 → 无水印 mp4（画质可选）；图文 → 全部图片。"""
    link = _require_link(params)
    cookie = (params.get("cookie") or "").strip()
    quality = (params.get("quality") or "original").strip().lower()
    progress = params.get("_progress")   # 壳注入的进度回调，前端进度弹窗轮询展示
    aweme_id = extract_aweme_id(link)
    paths, _kind = download_aweme(aweme_id, workdir, cookie=cookie,
                                  quality=quality, progress=progress)
    return [str(p) for p in paths]


def act_download_audio(files, params, workdir: Path):
    """按链接/ID 提取音频：视频抽音轨，图文提取背景音乐。"""
    link = _require_link(params)
    fmt = (params.get("format") or "flac").strip()
    cookie = (params.get("cookie") or "").strip()
    progress = params.get("_progress")
    aweme_id = extract_aweme_id(link)
    paths, _kind = download_aweme(aweme_id, workdir, cookie=cookie,
                                  mode="audio", audio_fmt=fmt, progress=progress)
    return [str(p) for p in paths]


def act_transcribe(files, params, workdir: Path):
    """下载抖音音轨并转写为文字（默认不保留音视频，只给文字）。

    管线：download_aweme(mode=audio) 取音轨 → whisperx 转写 → 输出 txt（纯文字）。
    中间的音频是「过路」临时文件，随任务结束被壳自动清理。
    """
    link = _require_link(params)
    cookie = (params.get("cookie") or "").strip()
    model_size = (params.get("model_size") or "small").strip()
    language = (params.get("language") or "").strip() or None
    progress = params.get("_progress")

    if progress:
        progress(percent=5, message="下载音轨…")
    aweme_id = extract_aweme_id(link)
    audio_paths, _kind = download_aweme(
        aweme_id, workdir, cookie=cookie, mode="audio", audio_fmt="flac", progress=progress
    )
    if not audio_paths:
        raise RuntimeError("未获取到音频，无法转写（抖音图文作品只有背景音乐，可能无语音）")
    audio_path = Path(audio_paths[0])

    return transcribe_audio(
        audio_path, workdir,
        model_size=model_size, language=language,
        progress=progress,
    )


def act_transcribe_file(files, params, workdir: Path):
    """上传本地音视频 → 转写为文字（复用同一套 WhisperX 管线）。"""
    uploaded = (files or {}).get("files", [])
    if not uploaded:
        raise ValueError("请先选择要转写的音视频文件")
    audio_path = Path(uploaded[0])
    model_size = (params.get("model_size") or "small").strip()
    language = (params.get("language") or "").strip() or None
    progress = params.get("_progress")

    return transcribe_audio(
        audio_path, workdir,
        model_size=model_size, language=language,
        progress=progress,
    )


ACTIONS = {
    "login_logout": act_login_logout,
    "download_video": act_download_video,
    "download_audio": act_download_audio,
    "transcribe": act_transcribe,
    "transcribe_file": act_transcribe_file,
}
