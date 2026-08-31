"""MD转公文Word插件：将 scripts/ 下的 md_to_word_formal.py 包装为统一插件动作。"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from md_to_word_formal import convert_md_to_docx  # noqa: E402


def act_md_to_docx(files, params, workdir: Path):
    fl = files.get("files") or []
    if not fl:
        raise ValueError("未收到输入文件")
    src = Path(fl[0])
    md_text = src.read_text(encoding="utf-8", errors="replace")
    out = workdir / f"{src.stem}.docx"
    convert_md_to_docx(md_text, out)
    return [out]


ACTIONS = {
    "md_to_docx": act_md_to_docx,
}
