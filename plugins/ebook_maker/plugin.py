"""电子书制作插件：将 scripts/ 下的成熟转换脚本包装为统一的插件动作。

本文件是壳与转换脚本之间的"翻译层"：
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
from txt_to_markdown import convert_txt_to_md  # noqa: E402
from markdown_to_epub import convert_md_to_epub  # noqa: E402
from ebook_maker import make_ebook, _setup_import_path  # noqa: E402
from epub_to_markdown import epub_to_markdown  # noqa: E402

_setup_import_path()  # 让 ebook_maker.py 能 import 同目录兄弟模块


def _first_file(files: dict) -> Path:
    fl = files.get("files") or []
    if not fl:
        raise ValueError("未收到输入文件")
    return Path(fl[0])


def act_txt_to_md(files, params, workdir: Path):
    src = _first_file(files)
    out = workdir / f"{src.stem}.md"
    convert_txt_to_md(src, out)
    return [out]


def act_md_to_epub(files, params, workdir: Path):
    src = _first_file(files)
    title = params.get("title") or src.stem
    out = workdir / f"{title}.epub"
    convert_md_to_epub(
        md_file=src, output_file=out, cover_file=params.get("cover") or None,
        title=title, author=params.get("author", ""),
        publisher=params.get("publisher", ""), isbn=params.get("isbn", ""),
        date=params.get("date", ""), description=params.get("description", ""),
        rights=params.get("rights", ""), subject=params.get("subject", ""),
        no_check=False,
    )
    return [out]


def act_txt_to_epub(files, params, workdir: Path):
    src = _first_file(files)
    title = params.get("title") or src.stem
    out = workdir / f"{title}.epub"
    make_ebook(
        input_path=src, output_path=out, title=title,
        author=params.get("author", ""), cover=params.get("cover") or None,
        publisher=params.get("publisher", ""), isbn=params.get("isbn", ""),
        date=params.get("date", ""), description=params.get("description", ""),
        keep_md=False, no_check=False, dry_run=False, verbose=False,
    )
    return [out]


def act_epub_to_md(files, params, workdir: Path):
    src = _first_file(files)
    out = workdir / f"{src.stem}.md"
    epub_to_markdown(src, output_file=out)
    return [out]


ACTIONS = {
    "txt_to_md": act_txt_to_md,
    "md_to_epub": act_md_to_epub,
    "txt_to_epub": act_txt_to_epub,
    "epub_to_md": act_epub_to_md,
}
