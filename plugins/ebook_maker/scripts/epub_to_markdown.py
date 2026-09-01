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


# 章节标题识别：Calibre 等工具常把 <p id="toc-anchor-N">第X章 标题</p> 当作普通段落，
# 这里把它升级为 <h2>，让转换器能正确识别章节分级。
_CHAPTER_TITLE_RE = re.compile(
    r'^(楔子|引子|序章|序言|前言|后记|附录|番外|第[一二三四五六七八九十百千零两0-9]+[章回卷部节篇集])'
    r'|^(Chapter|CHAPTER)\s+[0-9]+',
)


def _promote_chapter_headings(soup):
    """将 Calibre 等以普通 <p>/<div>/<span> 标签 + id="toc-anchor*" 标记、但文本实为章节标题的元素，升级为 <h2> 标题。

    Calibre 生成的 EPUB 用 <p id="toc-anchor-1">第二章 海内存知己</p> 同时充当"目录跳转锚点"和"章节标题"，
    但它不是真正的 <h2> 标签，html2text 只会把它当普通文字，导致正文里 27 个章节标题全部塌成普通段落。
    这里识别出来并升级，使章节恢复分级。
    """
    for el in soup.find_all(attrs={'id': re.compile(r'toc-anchor')}):
        txt = el.get_text(strip=True)
        if not txt or not _CHAPTER_TITLE_RE.match(txt):
            continue
        # 清掉内部多余标签（如 <br>），只保留标题文字，避免 html2text 输出多余空行
        el.clear()
        el.append(txt)
        el.name = 'h2'


_FRONT_MATTER_RE = re.compile(r'(公众号|版权|出版|CIP|ISBN|整理|内部交流|购买正版|免费领取|目录|目\s*录)')


def _looks_like_title(text):
    """判断一段文字是否像"章节/小节标题"，而非正文段落、对话或推广语。

    用于决定单节文档是否要包一层 ## {title}：只有像标题的才包，
    否则（版权页、公众号推广语、章节被拆分后下半截以对话开头等）当作普通正文，避免生成假标题。
    """
    if not text:
        return False
    if len(text) > 25:          # 太长，像正文/对话
        return False
    if re.search(r'[。！？；：、“”‘’…—]', text):  # 含句子标点，像正文
        return False
    if _FRONT_MATTER_RE.search(text):  # 推广/版权/目录关键词
        return False
    return True


def _normalize_single_h1(md_text, book_name):
    """保证全书只有一个一级标题（# 书籍名）。

    源 EPUB 常用 <h1> 标注章节标题，html2text 会原样转成 '#'，与插入的书籍名一级标题冲突，
    导致出现多个一级标题。这里把正文中除"首个书籍名"外的所有 '# ' 标题降级为 '## '，
    使层级统一为 书 > 章 > 节（无论源文件用 <h1> 还是 <h2> 标章节，最终都归一到 ##）。

    注意：只保留第一个 '# {book_name}'（即插入的书籍名），其后即使出现与书名同题的章节
    （如散文集首篇恰与书名同名），也一律降级，避免出现第二个一级标题。
    """
    out = []
    book_seen = False
    for ln in md_text.split('\n'):
        if ln == f'# {book_name}':
            if book_seen:
                out.append('#' + ln)  # '# X' -> '## X'
            else:
                out.append(ln)
                book_seen = True
            continue
        if ln.startswith('# ') and not ln.startswith('## '):
            out.append('#' + ln)  # '# X' -> '## X'
        else:
            out.append(ln)
    return '\n'.join(out)


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
            # 将 Calibre 等以普通标签 + id="toc-anchor*" 标记的章节标题升级为 <h2>
            _promote_chapter_headings(soup)
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

            # 纯目录页判定：含目录容器/标题且转换后"没有任何章节标题（## ）"→ 视为纯目录页跳过，
            # 避免一堆指向 EPUB 内部文件的坏链接被当成正文。
            # 注意：单文件 EPUB 常把目录和全文放在同一文档，此时 md_text 含章节标题，应保留。
            _is_pure_toc = ('id="toc"' in html_content and html_content.count('href') > 3) or \
                           (chapter_title.strip() in ('目录', '目 录', 'Table of Contents', 'Contents')
                            and html_content.count('href') > 3)
            if _is_pure_toc and ('## ' not in md_text and '# ' not in md_text):
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

    # 一级标题固定为书籍名称（H1）：取 EPUB 的 DC title，取不到时回退到文件名
    book_name = (metadata.get('title') or '').strip() or os.path.splitext(os.path.basename(epub_file))[0]
    md_lines.append(f"# {book_name}")
    md_lines.append("")

    # 正文
    for title_text, md_body in all_chapters:
        # 如果章节标题不存在，尝试从正文第一行提取
        if not title_text:
            first_line = md_body.split('\n', 1)[0].strip().lstrip('#').strip()
            title_text = first_line if first_line else "未命名章节"

        # 正文自身已带标题（如 Calibre 的 toc-anchor 章节被升级为 ##，或多章节同文件）：
        # 不再额外包一层 ## {title}，避免重复/错位
        if re.search(r'(?m)^#{1,6}\s', md_body):
            md_lines.append(md_body)
            md_lines.append("")
            continue

        # 单节文档：仅当标题像"标题"（简短、无句子标点、非推广/版权词）才包一层 ##；
        # 否则（版权页、公众号推广语、章节下半截的对话开头等）当作普通正文追加，不生成假标题
        if _looks_like_title(title_text):
            md_lines.append(f"## {title_text}")
            md_lines.append("")
            md_lines.append(md_body)
            md_lines.append("")
        else:
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
        # 统一一级标题：全书仅保留一个 # 书籍名，其余源文件以 <h1> 标注的章节标题降级为 ##
        final_md = _normalize_single_h1(final_md, book_name)
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
                save_dir = os.path.dirname(str(output_file)) or '.'

            # 确定保存的文件名和扩展名
            ext = os.path.splitext(cover_item.get_name())[1].lower()
            # 修正一些特殊扩展名
            if ext in ('.jpe', '.jfif'):
                ext = '.jpg'
            # 封面文件名以"实际生成的 md 输出路径"的 stem 为准（而非原始 epub 名），
            # 这样批量转换时同名书籍不会互相覆盖封面
            if (not split_chapters) and output_file and os.path.isfile(str(output_file)):
                cover_base = os.path.splitext(os.path.basename(str(output_file)))[0]
            else:
                cover_base = base_name
            cover_file = os.path.join(save_dir, f"{cover_base}_cover{ext}")

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

    # 返回结果：md 为单文件/分章列表，cover 为封面路径或 None
    # 插件层据此把 md 与封面一起返回给前端供下载
    result = {"md": (saved_files if split_chapters else output_file), "cover": cover_saved_path}
    return result


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