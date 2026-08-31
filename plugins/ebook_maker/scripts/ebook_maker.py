#!/usr/bin/env python3
"""
电子书制作工具（CLI版）

一站式电子书制作：支持 TXT / EPUB / Markdown 三种输入格式，
自动转换为 EPUB 电子书并验证章节连续性。

用法:
    python ebook_maker.py <输入文件> [选项]

示例:
    python ebook_maker.py "D:\\Books\\novel.txt"
    python ebook_maker.py "D:\\Books\\novel.txt" --title "书名" --author "作者" --cover "D:\\covers\\cover.jpg"
    python ebook_maker.py "D:\\Books\\book.epub" -o "D:\\Output\\remastered.epub" --keep-md
    python ebook_maker.py "D:\\Books\\draft.md" --no-check
    python ebook_maker.py "D:\\Books\\novel.txt" --dry-run

依赖:
    pip install ebooklib markdown beautifulsoup4 html2text
"""

import os
import sys
import argparse
from pathlib import Path


def _setup_import_path():
    """将本脚本所在目录加入 sys.path，以便 import 同目录工具模块。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)


def _extract_epub_metadata(epub_path):
    """直接从 EPUB 提取元数据，返回 dict。"""
    from ebooklib import epub
    book = epub.read_epub(str(epub_path))
    meta = {}
    try:
        title = book.get_metadata('DC', 'title')
        meta['title'] = title[0][0] if title else ''
    except Exception:
        meta['title'] = ''
    try:
        creator = book.get_metadata('DC', 'creator')
        meta['author'] = creator[0][0] if creator else ''
    except Exception:
        meta['author'] = ''
    try:
        publisher = book.get_metadata('DC', 'publisher')
        meta['publisher'] = publisher[0][0] if publisher else ''
    except Exception:
        meta['publisher'] = ''
    try:
        date = book.get_metadata('DC', 'date')
        meta['date'] = date[0][0] if date else ''
    except Exception:
        meta['date'] = ''
    try:
        description = book.get_metadata('DC', 'description')
        meta['description'] = description[0][0] if description else ''
    except Exception:
        meta['description'] = ''
    return meta


def _validate_chapters(md_path, verbose=False):
    """使用 ebook_chapter_validator 检查章节连续性。"""
    import ebook_chapter_validator as validator
    chapters, _ = validator.extract_chapters(Path(md_path), show_progress=False)
    result = validator.check_continuity(chapters)
    report = validator.generate_report(Path(md_path), chapters, result, verbose=verbose)
    return report, result


def make_ebook(input_path, output_path=None, title="", author="",
               cover=None, publisher="", isbn="", date="",
               description="", keep_md=False, no_check=False,
               dry_run=False, verbose=False):
    """
    主编排函数：输入 → Markdown → EPUB + 验证。

    Args:
        input_path: 输入文件路径（Path 对象）
        output_path: 输出 EPUB 路径（Path 对象，可选）
        title/author/cover/...: 元数据参数
        keep_md: 是否保留中间 Markdown 文件
        no_check: 是否跳过章节验证
        dry_run: 预览模式
        verbose: 详细输出

    Returns:
        bool: 是否成功
    """
    input_path = Path(input_path).resolve()
    ext = input_path.suffix.lower()

    # --- 确定输出路径 ---
    if output_path:
        output_path = Path(output_path).resolve()
    else:
        output_path = input_path.with_suffix('.epub')
    if output_path.suffix.lower() != '.epub':
        output_path = output_path.with_suffix('.epub')

    # --- 确定中间 MD 路径 ---
    md_path = input_path.with_name(input_path.stem + '_中间稿.md')

    if dry_run:
        print(f"[预览] 输入: {input_path}  ({ext})")
        print(f"[预览] 中间 Markdown: {md_path}")
        print(f"[预览] 输出 EPUB: {output_path}")
        if not no_check:
            print(f"[预览] 制作完成后将进行章节连续性验证")
        if not keep_md:
            print(f"[预览] 制作完成后将删除中间 Markdown")
        return True

    # --- 步骤 1：转为 Markdown ---
    print(f"[1/3] 输入预处理...")

    if ext == '.txt':
        import txt_to_markdown as t2m
        print(f"  检测到 TXT 文件，执行 TXT → Markdown ...")
        success = t2m.convert_txt_to_md(input_path, md_path, dry_run=False, verbose=verbose)
        if not success:
            print("[错误] TXT 转 Markdown 失败")
            return False

    elif ext == '.epub':
        import epub_to_markdown as e2m
        print(f"  检测到 EPUB 文件，执行 EPUB → Markdown ...")
        e2m.epub_to_markdown(str(input_path), output_file=str(md_path),
                             include_metadata=True, split_chapters=False)
        # epub_to_markdown 没有返回值，检查文件是否生成
        if not md_path.exists():
            print("[错误] EPUB 转 Markdown 失败：未生成中间文件")
            return False

    elif ext in ('.md', '.markdown'):
        print(f"  检测到 Markdown 文件，跳过转换步骤。")
        # 直接复制/链接？用户可能希望保留原始 MD，这里直接使用原始文件
        # 如果 keep_md 为 False 且是原始 MD，不应删除。用原始路径。
        md_path = input_path

    else:
        print(f"[错误] 不支持的输入格式: {ext}（仅支持 .txt / .epub / .md）")
        return False

    if not md_path.exists():
        print(f"[错误] 未找到可用的 Markdown 文件: {md_path}")
        return False

    # --- 步骤 2：Markdown → EPUB ---
    print(f"[2/3] 生成 EPUB ...")
    import markdown_to_epub as m2e

    m2e.convert_md_to_epub(
        md_file=str(md_path),
        output_file=str(output_path),
        cover_file=cover,
        title=title,
        author=author,
        publisher=publisher,
        isbn=isbn,
        date=date,
        description=description,
    )

    if not output_path.exists():
        print("[错误] EPUB 生成失败")
        return False

    # --- 步骤 3：章节验证 ---
    if not no_check:
        print(f"[3/3] 章节连续性验证...")
        # 用中间 MD 做验证（如果是原始 MD 输入也一样）
        report, result = _validate_chapters(str(md_path), verbose=verbose)
        print(report)
    else:
        print(f"[3/3] 已跳过章节验证（--no-check）")

    # --- 清理 ---
    if not keep_md and ext != '.md' and md_path != input_path:
        if md_path.exists():
            md_path.unlink()
            if verbose:
                print(f"[清理] 已删除中间文件: {md_path}")

    # --- 结果 ---
    print(f"\n制作完成: {output_path}")
    return True


def main():
    """CLI 入口"""
    _setup_import_path()

    parser = argparse.ArgumentParser(
        description="一站式电子书制作工具：支持 TXT / EPUB / Markdown 输入，自动生成 EPUB 并验证章节连续性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python ebook_maker.py "D:\\Books\\novel.txt"
    python ebook_maker.py "D:\\Books\\novel.txt" --title "书名" --author "作者" --cover "D:\\covers\\cover.jpg"
    python ebook_maker.py "D:\\Books\\book.epub" -o "D:\\Output\\remastered.epub" --keep-md
    python ebook_maker.py "D:\\Books\\draft.md" --no-check
    python ebook_maker.py "D:\\Books\\novel.txt" --dry-run
        """,
    )

    # 必需参数
    parser.add_argument(
        "input",
        help="输入文件路径（支持 .txt / .epub / .md 格式）",
    )

    # 输出选项
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="输出 EPUB 文件路径（默认：与输入同目录、同名、.epub 后缀）",
    )

    # 元数据选项
    parser.add_argument(
        "--title",
        default="",
        help="电子书书名（默认：自动从输入文件推断）",
    )
    parser.add_argument(
        "--author",
        default="",
        help="作者名称",
    )
    parser.add_argument(
        "--cover",
        default=None,
        help="封面图片路径或 URL（支持 http/https）",
    )
    parser.add_argument(
        "--publisher",
        default="",
        help="出版社名称",
    )
    parser.add_argument(
        "--isbn",
        default="",
        help="ISBN 编号",
    )
    parser.add_argument(
        "--date",
        default="",
        help="出版日期（如 2025-01-01）",
    )
    parser.add_argument(
        "--description",
        default="",
        help="书籍简介/摘要",
    )

    # 行为选项
    parser.add_argument(
        "--keep-md",
        action="store_true",
        help="保留中间生成的 Markdown 文件（默认：制作完成后自动删除）",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="跳过制作完成后的章节连续性验证",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式：显示将要执行的操作，不实际执行",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="详细输出模式",
    )

    args = parser.parse_args()

    # 参数验证
    input_path = Path(args.input)
    if not args.dry_run and not input_path.exists():
        print(f"[错误] 文件不存在: {args.input}")
        sys.exit(1)
    if not args.dry_run and not input_path.is_file():
        print(f"[错误] 不是有效文件: {args.input}")
        sys.exit(1)

    ext = input_path.suffix.lower()
    if ext not in ('.txt', '.epub', '.md', '.markdown'):
        print(f"[错误] 不支持的文件格式: {ext}（仅支持 .txt / .epub / .md）")
        sys.exit(1)

    # EPUB 输入时，如果用户未指定元数据，自动提取
    title = args.title
    author = args.author
    publisher = args.publisher
    date = args.date
    description = args.description

    if ext == '.epub' and not args.dry_run and (not title or not author):
        try:
            meta = _extract_epub_metadata(input_path)
            if not title and meta.get('title'):
                title = meta['title']
            if not author and meta.get('author'):
                author = meta['author']
            if not publisher and meta.get('publisher'):
                publisher = meta['publisher']
            if not date and meta.get('date'):
                date = meta['date']
            if not description and meta.get('description'):
                description = meta['description']
            if args.verbose:
                print(f"[INFO] 从 EPUB 提取元数据: title={title}, author={author}")
        except Exception as e:
            if args.verbose:
                print(f"[WARN] 无法提取 EPUB 元数据: {e}")

    try:
        success = make_ebook(
            input_path=input_path,
            output_path=args.output,
            title=title,
            author=author,
            cover=args.cover,
            publisher=publisher,
            isbn=args.isbn,
            date=date,
            description=description,
            keep_md=args.keep_md,
            no_check=args.no_check,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        if not success:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n[INFO] 操作被用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"[错误] 处理失败: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
