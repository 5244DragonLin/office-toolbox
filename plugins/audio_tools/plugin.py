"""音频切分工具插件：将 scripts/ 下的 audio_splitter.py 包装为统一的插件动作。

本文件是壳与切分脚本之间的"翻译层"：
- 每个动作函数签名统一为 fn(files, params, workdir) -> list[Path]
- files:  {"files": [已保存的上传文件路径, ...]}
- params: 前端表单提交的参数 dict
- workdir: 本次任务专属临时目录，输出文件应写在这里
"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# 首次 import 失败时，壳会自动按 requirements.txt 安装缺失依赖并重试
from audio_splitter import split_audio  # noqa: E402


def _first_file(files: dict) -> Path:
    fl = files.get("files") or []
    if not fl:
        raise ValueError("未收到输入文件")
    return Path(fl[0])


def act_split_audio(files, params, workdir: Path):
    """上传音频，按时间点 / 等长片段切分，返回切分后的所有片段文件。"""
    src = _first_file(files)
    return split_audio(src, params, workdir)


ACTIONS = {
    "split_audio": act_split_audio,
}
