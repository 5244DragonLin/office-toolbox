"""抖音下载插件：将 scripts/ 下的 douyin_fetch.py 包装为统一插件动作。"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from douyin_fetch import extract_audio, extract_aweme_id, download_video  # noqa: E402


def _require_link(params: dict) -> str:
    link = (params.get("link") or "").strip()
    if not link:
        raise ValueError("请填写抖音链接或视频 ID（必填项）")
    return link


def act_download_video(files, params, workdir: Path):
    """按链接/ID 下载无水印视频。"""
    link = _require_link(params)
    aweme_id = extract_aweme_id(link)
    return [download_video(aweme_id, workdir)]


def act_download_audio(files, params, workdir: Path):
    """按链接/ID 下载音频（先下视频再抽音频）。"""
    link = _require_link(params)
    fmt = (params.get("format") or "flac").strip()
    aweme_id = extract_aweme_id(link)
    mp4 = download_video(aweme_id, workdir)
    return [extract_audio(mp4, workdir, fmt)]


ACTIONS = {
    "download_video": act_download_video,
    "download_audio": act_download_audio,
}
