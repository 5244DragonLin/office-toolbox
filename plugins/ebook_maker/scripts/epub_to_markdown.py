#!/usr/bin/env python3
"""
EPUB 转 Markdown 工具（CLI 版）

将 EPUB 电子书各章节提取并转换为 Markdown 文件，支持输出元数据并提取封面图片。

依赖:
    pip install ebooklib beautifulsoup4 html2text

用法:
    python epub_to_markdown.py input.epub
    python epub_to_markdown.py input.epub -o output.md
    python epub_to_markdown.py input.epub --no-metadata
    python epub_to_markdown.py input.epub --split           # 每个章节单独输出一个 .md 文件
    python epub_to_markdown.py input.epub --no-cover        # 不提取封面图片
"""

import os
import re
import argparse
from ebooklib import epub
from bs4 import BeautifulSoup
import html2text


def epub_to_markdown(epub_file, output_file=None, include_metadata=True, split_chapters=False, extract_cover=True):
    """将 EPUB 转换为 Markdown"""
    if not os.path.exists(epub_file):
        print(f"错误：EPUB 文件不存在: {epub_file}")
        return False

    print(f"正在读取: {epub_file} ...")
    import time
    t0 = time.time()

    try:
        book = epub.read_epub(epub_file)
    except Exception as e:
        print(f"读取 EPUB 失败: {e}")
        return False
    print(f"  read_epub 完成，耗时 {time.time()-t0:.1f}s，开始提取元数据...")

    # --- 提取元数据 ---
    metadata = {}
    try:
        title = book.get_metadata('DC', 'title')
        metadata['title'] = title[0][0] if title else ''
    except Exception:
        metadata['title'] = ''
    try:
        creator = book.get_metadata('DC', 'creator')
        metadata['author'] = creator[0][0] if creator else ''
    except Exception:
        metadata['author'] = ''
    try:
        publisher = book.get_metadata('DC', 'publisher')
        metadata['publisher'] = publisher[0][0] if publisher else ''
    except Exception:
        metadata['publisher'] = ''
    try:
        date = book.get_metadata('DC', 'date')
        metadata['date'] = date[0][0] if date else ''
    except Exception:
        metadata['date'] = ''
    try:
        description = book.get_metadata('DC', 'description')
        metadata['description'] = description[0][0] if description else ''
    except Exception:
        metadata['description'] = ''
    try:
        rights = book.get_metadata('DC', 'rights')
        metadata['rights'] = rights[0][0] if rights else ''
    except Exception:
        metadata['rights'] = ''
    try:
        subject = book.get_metadata('DC', 'subject')
        metadata['subject'] = subject[0][0] if subject else ''
    except Exception:
        metadata['subject'] = ''

    print(f"  元数据: title={metadata['title'][:30] if metadata['title'] else '(无)'}, "
          f"author={metadata['author'][:20] if metadata['author'] else '(无)'}")
    print(f"  开始处理 spine，共 {len(book.spine)} 个条目...")

    # --- 配置 html2text ---
    h = html2text.HTML2Text()
    h.body_width = 0          # 不自动换行
    h.ignore_links = False
    h.ignore_images = False
    h.ignore_emphasis = False
    h.ignore_tables = False
    h.protect_links = True
    h.unicode_snob = True
    h.mark_code = True

    # --- 提取封面图片 ---
    # ebooklib item types:
    #   9 = ITEM_DOCUMENT, 10 = ITEM_COVER, 20 = ITEM_IMAGE, 23 = ITEM SVG
    #   17 = ITEM_GUIDE, 4 = ITEM_NCX, 1 = ITEM_UNKNOWN
    _EPUB_TYPES = {}
    _IMAGE_TYPES = (9, 10, 20, 23, 1)  # 包含未知类型（炸裂志 type=1）
    _IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tiff')

    def _is_image(item_ref):
        """判断一个 item 是否是图片资源（包括 type=1 的未知类型）"""
        if item_ref.get_type() in _IMAGE_TYPES:
            fname = item_ref.get_name().lower()
            if any(ext in fname for ext in _IMAGE_EXTS):
                return True
        return False

    def _extract_cover_images_from_item(item_ref):
        """从封面页 HTML/XHTML 中提取 <img> 标签指向的图片"""
        found = []
        try:
            html_content = item_ref.get_content().decode('utf-8', errors='ignore')
            for m in re.finditer(r'<img[^>]*src=["\']([^"\']+)["\']', html_content):
                src = m.group(1)
                ref_item = book.get_item(src.split('#')[0])
                if ref_item and _is_image(ref_item):
                    found.append(ref_item)
            # 也检查 data 属性
            for m in re.finditer(r'<img[^>]*data=["\']([^"\']+)["\']', html_content):
                src = m.group(1)
                ref_item = book.get_item(src.split('#')[0])
                if ref_item and _is_image(ref_item):
                    found.append(ref_item)
        except Exception:
            pass
        return found

    cover_item = None

    # ========= 策略1: 直接查找 type=10 (ITEM_COVER) 的图片 =========
    for item_ref in book.get_items():
        if item_ref.get_type() == 10:  # ITEM_COVER
            cover_item = item_ref
            break

    # ========= 策略2: GUIDE 中的 cover-page 引用 =========
    if not cover_item:
        for item_ref in book.get_items():
            if item_ref.get_type() == 17:  # GUIDE
                content = item_ref.get_content()
                for m in re.finditer(rb'<reference[^>]*type="[^"]*cover[^"]*"[^>]*href="([^"]*)"', content):
                    href = m.group(1).decode('utf-8', errors='ignore')
                    ref_item = book.get_item(href.split('#')[0])
                    if ref_item and _is_image(ref_item):
                        cover_item = ref_item
                        break
                if cover_item:
                    break

    # ========= 策略3: 扫描所有图片资源，按文件名评分 =========
    if not cover_item:
        image_candidates = []
        for item_ref in book.get_items():
            if _is_image(item_ref):
                fname = item_ref.get_name().lower()
                score = 0
                if item_ref.get_type() == 10:
                    score = 10
                elif 'cover' in fname:
                    score = 8
                elif 'front' in fname or 'titlepage' in fname:
                    score = 6
                elif 'image' in fname or 'img' in fname:
                    score = 3
                elif item_ref.get_type() in (9, 1):  # DOCUMENT or UNKNOWN，出现在 spine 前面的可能是封面页内嵌图
                    score = 2
                image_candidates.append((score, item_ref))
        if image_candidates:
            image_candidates.sort(reverse=True, key=lambda x: x[0])
            cover_item = image_candidates[0][1]

    # ========= 策略4: 检查所有 type=9 的文档，查找"封面"页面中的 <img> =========
    if not cover_item:
        for item_ref in book.get_items():
            if item_ref.get_type() == 9:
                fname_lower = item_ref.get_name().lower()
                # 匹配 cover.xxx 或 封面.xxx
                fname_base = os.path.splitext(item_ref.get_name())[0].lower()
                if fname_base.endswith(('cover', 'coverpage', 'front', 'titlepage', '封面')):
                    embedded = _extract_cover_images_from_item(item_ref)
                    if embedded:
                        cover_item = embedded[0]
                        break

    # ========= 策略5: 扫描所有 type=9 的文档，解析 <img> 找第一个图片 =========
    if not cover_item:
        for item_ref in book.get_items():
            if item_ref.get_type() == 9:
                embedded = _extract_cover_images_from_item(item_ref)
                if embedded:
                    cover_item = embedded[0]
                    break

    # --- 提取脊柱中的章节内容 ---
    spine_ids = [item_ref[0] for item_ref in book.spine]
    all_chapters = []
    print(f"  开始处理 {len(spine_ids)} 个 spine 条目...")

    for i, item_id in enumerate(spine_ids):
        if i % 500 == 0:
            print(f"    处理进度: {i}/{len(spine_ids)}")
        try:
            item = book.get_item_with_id(item_id)
            if item is None:
                continue
            # 跳过非 HTML/XHTML 内容
            content_type = item.get_type()
            if content_type != 9:  # ebooklib.ITEM_DOCUMENT = 9
                continue
            # 跳过封面和内嵌样式
            file_name = item.get_name()
            if 'nav' in file_name.lower() or 'toc' in file_name.lower():
                continue

            html_content = item.get_content().decode('utf-8')

            soup = BeautifulSoup(html_content, 'xml')
            body = soup.find('body')
            if body is None:
                continue

            # 尝试从 body 或 title 中提取章节标题
            title_tag = soup.find('title')
            chapter_title = title_tag.get_text(strip=True) if title_tag else ''

            # 去掉封面页
            if chapter_title in ('封面', 'Cover', 'cover'):
                continue

            # 将 body 内容转为 Markdown
            body_html = str(body)
            md_text = h.handle(body_html).strip()

            if not md_text:
                continue

            all_chapters.append((chapter_title, md_text))
        except Exception as e:
            print(f"    [警告] 处理条目 {item_id} 时出错: {e}")
            continue

    print(f"  处理完成，共提取 {len(all_chapters)} 个章节")

    if not all_chapters:
        print("警告：未提取到任何章节内容。")
        return False

    # --- 构建 Markdown 输出 ---
    md_lines = []

    # 元数据块
    if include_metadata:
        md_lines.append("---")
        if metadata['title']:
            md_lines.append(f"title: {metadata['title']}")
        if metadata['author']:
            md_lines.append(f"author: {metadata['author']}")
        if metadata['publisher']:
            md_lines.append(f"publisher: {metadata['publisher']}")
        if metadata['date']:
            md_lines.append(f"date: {metadata['date']}")
        if metadata['description']:
            md_lines.append(f"description: {metadata['description']}")
        if metadata['rights']:
            md_lines.append(f"rights: {metadata['rights']}")
        if metadata['subject']:
            md_lines.append(f"subject: {metadata['subject']}")
        md_lines.append("---")
        md_lines.append("")

    # 正文
    for title_text, md_body in all_chapters:
        # 如果章节标题不存在，尝试从正文第一行提取
        if not title_text:
            first_line = md_body.split('\n', 1)[0].strip().lstrip('#').strip()
            title_text = first_line if first_line else "未命名章节"

        # 检查 md_body 的首行是否已是标题（避免重复输出）
        md_first_line = md_body.split('\n', 1)[0].strip()
        if md_first_line.startswith('## ') or md_first_line.startswith('# '):
            md_first_title = md_first_line.lstrip('#').strip()
            # 如果 body 中的标题与提取的 title_text 一致（忽略"## "前缀和额外空格），则不重复添加
            if title_text and md_first_title == title_text.strip():
                # 已有匹配标题，跳过添加
                md_lines.append(md_body)
                md_lines.append("")
                continue

        md_lines.append(f"## {title_text}")
        md_lines.append("")
        md_lines.append(md_body)
        md_lines.append("")

    final_md = '\n'.join(md_lines)

    # --- 清理多余空行 ---
    final_md = re.sub(r'\n{3,}', '\n\n', final_md)
    final_md = final_md.strip() + '\n'

    # --- 输出 ---
    base_name = os.path.splitext(os.path.basename(epub_file))[0]

    if split_chapters:
        # 每个章节单独输出
        output_dir = os.path.dirname(output_file) if output_file else os.path.dirname(epub_file)
        if not output_dir:
            output_dir = '.'
        saved_files = []
        skipped_files = []
        for i, (title_text, md_body) in enumerate(all_chapters):
            safe_title = re.sub(r'[\\/*?:"<>|]', '', title_text or f'chapter_{i+1:03d}')
            safe_title = safe_title.strip().replace(' ', '_') or f'chapter_{i+1:03d}'
            chap_file = os.path.join(output_dir, f'{base_name}_{i+1:03d}_{safe_title}.md')

            chap_lines = []
            if include_metadata:
                chap_lines.append("---")
                if metadata['title']:
                    chap_lines.append(f"title: {metadata['title']} - {title_text}")
                chap_lines.append("---")
                chap_lines.append("")
            chap_lines.append(f"# {title_text}")
            chap_lines.append("")
            chap_lines.append(md_body)

            chap_md = '\n'.join(chap_lines)
            chap_md = re.sub(r'\n{3,}', '\n\n', chap_md).strip() + '\n'

            with open(chap_file, 'w', encoding='utf-8') as f:
                f.write(chap_md)
            saved_files.append(chap_file)
        print(f"已输出 {len(saved_files)} 个章节文件到: {output_dir}")
        for f in saved_files:
            print(f"  {f}")
    else:
        # 输出单个文件
        if output_file is None:
            output_file = os.path.join(os.path.dirname(epub_file) or '.', f'{base_name}.md')
        else:
            # 智能判断：如果 output_file 是目录，则自动追加 {base_name}.md
            if os.path.isdir(output_file):
                output_file = os.path.join(output_file, f'{base_name}.md')
                print(f"  检测到 -o 为目录，自动追加文件名: {output_file}")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_md)
        print(f"Markdown 文件已生成: {os.path.abspath(output_file)}")

    # --- 提取并保存封面图片 ---
    cover_saved_path = None
    if extract_cover and cover_item:
        try:
            # 确定输出目录
            if split_chapters:
                save_dir = output_dir
            else:
                save_dir = os.path.dirname(output_file) or '.'

            # 确定保存的文件名和扩展名
            orig_name = os.path.splitext(os.path.basename(cover_item.get_name()))[0]
            ext = os.path.splitext(cover_item.get_name())[1].lower()
            # 修正一些特殊扩展名
            if ext in ('.jpe', '.jfif'):
                ext = '.jpg'
            cover_file = os.path.join(save_dir, f"{base_name}_cover{ext}")

            cover_data = cover_item.get_content()
            with open(cover_file, 'wb') as cf:
                cf.write(cover_data)
            cover_saved_path = cover_file
            print(f"封面图片已提取: {os.path.abspath(cover_file)}")
        except Exception as e:
            print(f"  警告: 封面提取失败: {e}")
    else:
        print("  未检测到封面图片，跳过提取。")

    print(f"共提取 {len(all_chapters)} 个章节。")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="将 EPUB 电子书转换为 Markdown 文件"
    )
    parser.add_argument(
        "input",
        help="输入的 EPUB 文件路径"
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="输出 Markdown 文件路径（默认与输入同目录同名）"
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="不输出 YAML 元数据头"
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="按章节拆分，每个章节输出一个独立的 .md 文件"
    )
    parser.add_argument(
        "--no-cover",
        action="store_true",
        help="不提取封面图片"
    )

    args = parser.parse_args()

    try:
        epub_to_markdown(
            epub_file=args.input,
            output_file=args.output,
            include_metadata=not args.no_metadata,
            split_chapters=args.split,
            extract_cover=not args.no_cover,
        )
    except Exception as e:
        import traceback
        print(f"!!! 未捕获异常: {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()