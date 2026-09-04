"""B站下载插件：包装 yutto 的 B站下载能力为统一插件动作（单视频/批量/音频/字幕）。"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bilibili_fetch import (
    download_video,
    download_audio,
    download_favlist,
    download_collection,
    download_space,
    get_subtitle,
    get_video_title,
    login_start,
    login_poll,
    login_logout,
)

# 扫码登录契约：壳的 /api/login/{pid}/start|poll 路由会调用这两个函数，
# 前端弹窗展示二维码并轮询状态；logout 仍走普通动作返回提示文件。
LOGIN_PROVIDER = {
    "start": login_start,
    "poll": login_poll,
}


def _require_link(params: dict) -> str:
    link = (params.get("link") or "").strip()
    if not link:
        raise ValueError("请填写 B站视频链接（必填项）")
    return link


def _quality_of(params: dict) -> str:
    return (params.get("quality") or "best").strip()


def _sessdata_of(params: dict) -> str:
    return (params.get("sessdata") or "").strip()


def _progress_of(params: dict):
    """壳注入的进度回调 progress(percent, message)，不存在时返回 None。"""
    cb = params.get("_progress")
    return cb if callable(cb) else None


def act_download_video(files, params, workdir: Path):
    """下载 B站单个视频（完整 MP4，含画面+声音，保留原标题）。"""
    link = _require_link(params)
    return download_video(link, workdir, _quality_of(params), _sessdata_of(params),
                          progress=_progress_of(params))


def act_download_favlist(files, params, workdir: Path):
    """批量下载整个收藏夹。"""
    link = _require_link(params)
    return download_favlist(link, workdir, _quality_of(params), _sessdata_of(params),
                            progress=_progress_of(params))


def act_download_collection(files, params, workdir: Path):
    """批量下载合集/系列/播放列表。"""
    link = _require_link(params)
    return download_collection(link, workdir, _quality_of(params), _sessdata_of(params),
                               progress=_progress_of(params))


def act_download_space(files, params, workdir: Path):
    """批量下载 UP主空间全部投稿视频。"""
    link = _require_link(params)
    return download_space(link, workdir, _quality_of(params), _sessdata_of(params),
                          progress=_progress_of(params))


def act_download_audio(files, params, workdir: Path):
    """下载 B站视频并提取音频。"""
    link = _require_link(params)
    fmt = (params.get("format") or "mp3").strip()
    return [download_audio(link, workdir, fmt, _sessdata_of(params))]


def act_get_subtitle(files, params, workdir: Path):
    """提取 B站视频的字幕/文案。"""
    link = _require_link(params)
    return [get_subtitle(link, workdir, _sessdata_of(params))]


def act_login_logout(files, params, workdir: Path):
    """清除已保存的B站登录凭证。"""
    return [login_logout(workdir)]


ACTIONS = {
    "login_logout": act_login_logout,
    "download_video": act_download_video,
    "download_favlist": act_download_favlist,
    "download_collection": act_download_collection,
    "download_space": act_download_space,
    "download_audio": act_download_audio,
    "get_subtitle": act_get_subtitle,
}