#!/usr/bin/env python3
"""
Markdown 转 EPUB 工具（CLI 版）

将 Markdown 文件转换为 EPUB 电子书，支持元数据设置和封面图片。
支持多级别标题层级分组（# 卷 -> ## 章 -> ### 小节），自动生成分级目录。

用法:
    python markdown_to_epub.py input.md
    python markdown_to_epub.py input.md -o output.epub --title "书名" --author "作者"
    python markdown_to_epub.py input.md --cover cover.png --publisher "出版社" --isbn 123456
    python markdown_to_epub.py input.md --title "书名" --author "作者" --date "2025-01-01" --description "简介" --rights "版权所有" --subject "分类"
    python markdown_to_epub.py input.md --level 2  # 仅处理到二级标题（默认）

依赖:
    pip install ebooklib markdown
"""

import os
import re
import argparse
import urllib.request
import urllib.parse
from ebooklib import epub
from markdown import markdown


def is_url(path):
    """判断路径是否为 URL"""
    return path.startswith('http://') or path.startswith('https://')


def convert_md_to_epub(md_file, output_file, cover_file=None,
                       title="", author="", publisher="",
                       isbn="", contributor="水魅",
                       date="", description="", rights="", subject="",
                       no_check=False):
    """将 Markdown 文件转换为 EPUB"""
    if not os.path.exists(md_file):
        print(f"错误：Markdown 文件不存在: {md_file}")
        return False

    if cover_file and not is_url(cover_file) and not os.path.exists(cover_file):
        print(f"警告：封面文件不存在，将跳过封面: {cover_file}")
        cover_file = None

    print(f"正在转换: {md_file} ...")

    book = epub.EpubBook()

    book.set_identifier(isbn)
    book.set_title(title)
    book.set_language('zh-CN')
    if author:
        book.add_author(author)
    if publisher:
        book.add_metadata('DC', 'publisher', publisher)
    book.add_metadata('DC', 'contributor', contributor)
    if date:
        book.add_metadata('DC', 'date', date)
    if description:
        book.add_metadata('DC', 'description', description)
    if rights:
        book.add_metadata('DC', 'rights', rights)
    if subject:
        book.add_metadata('DC', 'subject', subject)

    # CSS 样式
    style_content = '''
    @namespace epub "http://www.idpf.org/2007/ops";
    body { font-family: "Songti SC", "SimSun", serif; line-height: 1.6; margin: 1em; }
    h1, h2 { text-align: center; margin-top: 1em; margin-bottom: 1em; }
    .chapter { page-break-after: always; }
    .cover-image { height: 100%; width: 100%; text-align: center; vertical-align: middle; display: block; }
    '''
    nav_css = epub.EpubItem(
        uid="style_nav", file_name="style/nav.css",
        media_type="text/css", content=style_content
    )
    book.add_item(nav_css)

    cover_page = None

    # 处理封面
    if cover_file:
        try:
            if is_url(cover_file):
                # 从 URL 下载封面图片
                with urllib.request.urlopen(cover_file, timeout=30) as resp:
                    cover_content = resp.read()
                # 从 URL 路径提取扩展名，无扩展名则默认为 .jpg
                url_path = urllib.parse.urlparse(cover_file).path
                ext = os.path.splitext(url_path)[1].lower()
                if not ext:
                    ext = '.jpg'
            else:
                with open(cover_file, 'rb') as f:
                    cover_content = f.read()
                ext = os.path.splitext(cover_file)[1].lower()

            book.set_cover(f"images/cover{ext}", cover_content)

            cover_html = f'''
            <!DOCTYPE html>
            <html xmlns="http://www.w3.org/1999/xhtml">
            <head><title>封面</title><link rel="stylesheet" type="text/css" href="style/nav.css"/></head>
            <body><div class="cover-image"><img src="images/cover{ext}" alt="封面"/></div></body>
            </html>
            '''
            cover_page = epub.EpubHtml(title='封面', file_name='cover_page.xhtml', lang='zh-CN')
            cover_page.content = cover_html
            cover_page.add_item(nav_css)
            book.add_item(cover_page)
            print("封面已添加")
        except Exception as e:
            print(f"封面处理失败: {e}")

    # 解析 Markdown（支持多级标题层级分组）
    with open(md_file, 'r', encoding='utf-8') as f:
        md_content = f.read()

    # ---- 第一步：扫描所有标题，记录位置 ----
    # 匹配 # (一级标题/卷) 和 ## (二级标题/章)
    all_headers = re.findall(
        r'^(#{1,2})\s+(.+)$', md_content, re.MULTILINE
    )

    # ---- 第二步：建立层级结构 ----
    # 结构: 每一卷是一个 EpubHtml 页面，包含一组子章节
    # 对于没有一级标题的书，所有二级标题都属于"无卷"
    volumes = []  # [(卷标题, [章节列表])]
    front_matter_pages = []  # 出版信息页

    # 按顺序逐段解析
    lines = md_content.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('# ') and not line.startswith('## '):
            # 一级标题：这是一卷（如 "第一卷 崛起之路"）
            vol_title = line[2:].strip()
            current_chapter_list = []
            volumes.append((vol_title, current_chapter_list))

            # 收集该卷标题行之后的所有内容（直到下一个标题或文件末尾）
            vol_content_lines = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith('## ') or nxt.startswith('# '):
                    break
                vol_content_lines.append(lines[j])
                j += 1
            vol_body = '\n'.join(vol_content_lines)

            # 创建卷标题页（始终创建，即使没有卷简介内容）
            vol_html_content = markdown(f'# {vol_title}')
            vol_page = epub.EpubHtml(
                title=vol_title,
                file_name=f'vol_{len(volumes)}.xhtml',
                lang='zh-CN'
            )
            vol_page.content = f'<div class="chapter">{vol_html_content}</div>'
            vol_page.add_item(nav_css)
            book.add_item(vol_page)
            current_chapter_list.append(vol_page)

            # 该卷标题行下的非标题内容作为卷简介正文（追加到卷标题页）
            if vol_body.strip():
                extra_md = markdown(vol_body)
                vol_page.content = f'<div class="chapter">{vol_html_content}</div><div class="chapter">{extra_md}</div>'

            i = j
            continue

        elif line.startswith('## '):
            # 二级标题：这是一章
            chap_title = line[3:].strip()

            # 确保 current_chapter_list 已定义（文件开头可能先遇到 ## 标题）
            if 'current_chapter_list' not in dir():
                current_chapter_list = []
                volumes.append(('', current_chapter_list))

            # 收集该章的内容（直到下一个标题或文件末尾）
            chap_content_lines = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith('## ') or nxt.startswith('# '):
                    break
                chap_content_lines.append(lines[j])
                j += 1
            chap_body = '\n'.join(chap_content_lines)

            md_full = f'## {chap_title}\n\n{chap_body}'
            html_content = markdown(md_full)
            final_html = f'<div class="chapter">{html_content}</div>'

            chap_num = len(current_chapter_list)
            chap = epub.EpubHtml(
                title=chap_title,
                file_name=f'vol{len(volumes)}_chap_{chap_num}.xhtml',
                lang='zh-CN'
            )
            chap.content = final_html
            chap.add_item(nav_css)
            book.add_item(chap)
            current_chapter_list.append(chap)
            i = j
            continue

        else:
            # 非标题行（可能是出版信息、简介等前置内容）
            info_content_lines = []
            j = i
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith('## ') or nxt.startswith('# '):
                    break
                info_content_lines.append(lines[j])
                j += 1
            info_body = '\n'.join(info_content_lines)

            if info_body.strip():
                info_md = f'{info_body.strip()}'
                info_html = markdown(info_md)
                info_page = epub.EpubHtml(
                    title='出版信息',
                    file_name=f'info_{len(front_matter_pages) + 1}.xhtml',
                    lang='zh-CN'
                )
                info_page.content = f'<div>{info_html}</div>'
                info_page.add_item(nav_css)
                book.add_item(info_page)
                front_matter_pages.append(info_page)

            i = j
            continue

    # 如果没有一级标题（卷），将所有章节合并为一个"无卷"组
    if not volumes:
        volumes = [('', current_chapter_list)]

    # ---- 章节连续性检查 ----
    if not no_check:
        check_chapter_continuity_from_volumes(volumes)

    # ---- 目录与 Spine ----
    # TOC: 分级目录，卷为顶层，章节为卷的子节点
    # ebooklib 的 book.toc 接受:
    #   - EpubHtml 对象（作为叶子节点）
    #   - (EpubHtml, [children]) 元组（作为分组节点）
    book.toc = []
    for vol_title, vol_items in volumes:
        if vol_title and len(vol_items) > 1:
            # 有卷且有多个项目：卷标题是父节点，第一页（卷简介）+ 后续章节作为子节点
            book.toc.append((vol_items[0], vol_items[1:]))
        elif vol_title:
            # 有卷但只有一个项目
            book.toc.append(vol_items[0])
        else:
            # 无卷模式：所有章节直接放在顶层
            book.toc.extend(vol_items)

    book.add_item(epub.EpubNcx())

    # Spine: 封面 -> 出版信息 -> 各卷标题页 -> 各卷章节
    spine = []
    if cover_page:
        spine.append(cover_page)

    for page in front_matter_pages:
        spine.append(page)

    for vol_title, vol_items in volumes:
        for item in vol_items:
            spine.append(item)

    book.spine = spine

    # 写入文件（使用 EPUB2 兼容模式，确保 NCX 目录正常跳转）
    epub.write_epub(output_file, book, {"epub2_guide": True})
    print(f"EPUB 文件已生成: {os.path.abspath(output_file)}")
    return True


# 中文数字映射
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}

def _chinese_to_arabic(cn_str):
    """中文数字转阿拉伯数字"""
    if not cn_str:
        return None
    if cn_str.isdigit():
        return int(cn_str)
    cn_str = cn_str.lstrip("零")
    if not cn_str:
        return 0
    total = 0
    current = 0
    for ch in cn_str:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            if current == 0:
                current = 1
            total += current * _CN_UNIT[ch]
            current = 0
    total += current
    return total

_CHAPTER_PATTERN = re.compile(r'第\s*([零一二三四五六七八九十百千\d]+)\s*章')

def check_chapter_continuity_from_volumes(volumes):
    """从 volumes 结构中提取章节标题，检查连续性"""
    from collections import Counter

    chapters = []
    for vol_title, vol_items in volumes:
        for item in vol_items:
            m = _CHAPTER_PATTERN.search(item.title)
            if m:
                num = _chinese_to_arabic(m.group(1))
                if num is not None:
                    chapters.append((num, item.title))

    if len(chapters) < 2:
        return

    numbers = [c[0] for c in chapters]
    min_num, max_num = min(numbers), max(numbers)
    expected = set(range(min_num, max_num + 1))
    actual = set(numbers)
    missing = sorted(expected - actual)

    counted = Counter(numbers)
    duplicates = {n: cnt for n, cnt in counted.items() if cnt > 1}

    if missing or duplicates:
        print(f"\n{'=' * 60}")
        print("章节连续性检查")
        print(f"{'=' * 60}")
        print(f"  共检测到 {len(chapters)} 个章节标题")
        print(f"  章节范围: 第 {min_num} 章 ~ 第 {max_num} 章")
        if missing:
            print(f"\n  ✗ 缺失章节 ({len(missing)} 个):")
            print("    " + ", ".join(f"第{n}章" for n in missing))
        if duplicates:
            print(f"\n  ✗ 重复章节 ({len(duplicates)} 处):")
            for n, cnt in sorted(duplicates.items()):
                print(f"    第{n}章 出现 {cnt} 次")
        print(f"{'=' * 60}\n")
    else:
        print(f"  ✓ 目录连续 ({len(chapters)} 章，未发现问题)\n")


def main():
    parser = argparse.ArgumentParser(
        description="将 Markdown 文件转换为 EPUB 电子书"
    )
    parser.add_argument(
        "input",
        help="输入的 Markdown 文件路径"
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="输出 EPUB 文件路径（默认与输入同目录同名）"
    )
    parser.add_argument(
        "--cover",
        default=None,
        help="封面图片路径或 URL（支持 http/https）"
    )
    parser.add_argument(
        "--title",
        default="",
        help="书名"
    )
    parser.add_argument(
        "--author",
        default="",
        help="作者"
    )
    parser.add_argument(
        "--publisher",
        default="",
        help="出版社"
    )
    parser.add_argument(
        "--isbn",
        default="",
        help="ISBN"
    )
    parser.add_argument(
        "--contributor",
        default="水魅",
        help="EPUB 制作者（默认：水魅）"
    )
    parser.add_argument(
        "--date",
        default="",
        help="出版日期（如 2025-01-01）"
    )
    parser.add_argument(
        "--description",
        default="",
        help="书籍简介/摘要"
    )
    parser.add_argument(
        "--rights",
        default="",
        help="版权信息"
    )
    parser.add_argument(
        "--subject",
        default="",
        help="主题/分类关键词"
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="跳过章节连续性检查"
    )

    args = parser.parse_args()

    # 确定输出路径
    if args.output:
        output_file = args.output
    else:
        base_name = os.path.splitext(os.path.basename(args.input))[0]
        output_file = os.path.join(os.path.dirname(args.input) or ".", f"{base_name}.epub")

    if not output_file.endswith('.epub'):
        output_file += '.epub'

    convert_md_to_epub(
        md_file=args.input,
        output_file=output_file,
        cover_file=args.cover,
        title=args.title,
        author=args.author,
        publisher=args.publisher,
        isbn=args.isbn,
        contributor=args.contributor,
        date=args.date,
        description=args.description,
        rights=args.rights,
        subject=args.subject,
        no_check=args.no_check
    )


if __name__ == "__main__":
    main()