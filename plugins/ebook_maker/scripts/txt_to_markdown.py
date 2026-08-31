#!/usr/bin/env python3
"""
TXT转Markdown工具（CLI版）

将小说/书籍TXT文件转换为Markdown格式，自动识别章节标题并转换为Markdown标题格式。
支持多种常见章节格式：第X章、第X节、Chapter X、数字编号、中文数字等。
转换完成后自动检查章节标题序号连续性，输出异常报告。

版本: v1.3 (2026-06-02)
  - v1.3: 修复段落换行符格式，每行后面加 \n\n 确保 MD 段落正确分隔；修复文档中路径转义警告
  - v1.2: 章节正则字符类补全「零」「两」，修复含零/两的章号无法识别的bug；新增"第X章（含标题）"规则的反误匹配过滤（后跟标点接长句或整行超50字跳过）
  - v1.1: 修复第X卷正则（支持"第一卷 崛起之路"格式）；改进中文数字解析（支持零、两）

用法:
    python txt_to_markdown.py input.txt
    python txt_to_markdown.py input.txt -o output.md
    python txt_to_markdown.py input.txt --dry-run

示例:
    python txt_to_markdown.py "D:/Books/novel.txt"
    python txt_to_markdown.py "D:/Books/novel.txt" -o "D:/Output/novel.md"
    python txt_to_markdown.py "D:/Books/novel.txt" --dry-run
    python txt_to_markdown.py "D:/Books/novel.txt" -v

依赖:
    无（仅使用Python标准库）
"""

import os
import re
import sys
import argparse
from pathlib import Path


# ============================================================
# 章节识别规则
# 每条规则 = (regex_pattern, markdown_level, description)
# markdown_level: 1=# 2=## 3=### 4=#### 5=#####
# 匹配时按列表顺序依次尝试，第一条匹配成功即生效
# ============================================================
CHAPTER_RULES = [
    # --- 一级标题（书名/大卷） ---
    # 第X卷 / 第X部（视为一级）
    (re.compile(r'^\s*第[零一二两三四五六七八九十百千万0-9]+[卷部][^=\n]*$'), 1, '第X卷/部'),
    # 前言 / 序言 / 后记 / 附录 等固定词
    (re.compile(r'^\s*(前言|序言|序章|序语|后记|尾声|附录|番外|外传|特典)\s*$'), 1, '固定词标题'),

    # --- 二级标题（章/Chapter） ---
    # 第X章（中文数字或阿拉伯数字）
    (re.compile(r'^\s*第[零一二两三四五六七八九十百千万0-9]+章\s*$'), 2, '第X章'),
    # 第X节（部分作品用"节"代替"章"）
    (re.compile(r'^\s*第[零一二两三四五六七八九十百千万0-9]+节\s*$'), 2, '第X节'),
    # Chapter X / CHAPTER X（英文格式）
    (re.compile(r'^\s*Chapter\s+[0-9]+\.?\s*$', re.IGNORECASE), 2, 'Chapter X'),
    # 第X章：标题名（带冒号后续文字）
    (re.compile(r'^\s*第[零一二两三四五六七八九十百千万0-9]+章[^=\n]*$'), 2, '第X章（含标题）'),
    # 【第X章】 方括号包裹
    (re.compile(r'^\s*【第[零一二两三四五六七八九十百千万0-9]+章[^】]*】\s*$'), 2, '【第X章】'),

    # --- 三级标题（节/小节） ---
    # 第X节（三级，当已出现章时使用）
    (re.compile(r'^\s*第[零一二两三四五六七八九十百千万0-9]+节\s*$'), 3, '第X节'),
    # 1.1 / 1.2 数字小节
    (re.compile(r'^\s*[0-9]+\.[0-9]+\s+[^=\n]*$'), 3, '数字小节 1.1'),
    # 一、二、三、 （中文数字+顿号）
    (re.compile(r'^\s*[零一二两三四五六七八九十]+[、．\.]\s*[^=\n]*$'), 3, '中文数字顿号'),
    # （一）（二）（三）（括号中文数字）
    (re.compile(r'^\s*[（(][零一二两三四五六七八九十][）)]\s*$'), 3, '括号中文数字'),

    # --- 四级标题（更小节） ---
    # 1.1.1
    (re.compile(r'^\s*[0-9]+\.[0-9]+\.[0-9]+\s+[^=\n]*$'), 4, '数字小节 1.1.1'),
    # 1.  2.  （数字+点+空格，独立行）
    (re.compile(r'^\s*[0-9]+[、．\.]\s+[^=\n]*$'), 4, '数字顿号/点'),
]

# 需要跳过的内容行（分隔线、纯符号行等）
SKIP_LINE_PATTERNS = [
    re.compile(r'^\s*[-=*_]{3,}\s*$'),           # 分隔线 --- === *** ___
    re.compile(r'^\s*[※☆★◆◇○●◆▼▲]+.*$'),     # 装饰符号行
]


def detect_encoding(file_path):
    """
    自动检测文件编码，依次尝试 utf-8、gbk、utf-16、big5。
    返回 (encoding, content_lines) 或 (None, None)。
    """
    for enc in ['utf-8-sig', 'gbk', 'utf-16', 'big5', 'gb18030']:
        try:
            with open(file_path, 'r', encoding=enc) as f:
                content = f.read()
                lines = content.splitlines()  # splitlines() 自动去除换行符（\r\n 或 \n）
            print(f"[INFO] 成功使用 {enc} 编码读取文件（共 {len(lines)} 行）")
            return enc, lines
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None, None


def is_chapter_line(line):
    """
    判断一行是否为章节标题行。
    返回 (is_chapter, level, rule_description) 或 (False, None, None)。
    """
    stripped = line.strip()
    if not stripped:
        return False, None, None

    # 先检查是否应跳过（分隔线等）
    for skip_pat in SKIP_LINE_PATTERNS:
        if skip_pat.match(stripped):
            return False, None, None

    # 依次尝试章节规则
    for pattern, level, desc in CHAPTER_RULES:
        if pattern.match(stripped):
            # 一级标题（第X卷/部）额外过滤：排除正文误匹配
            # 真正的卷部标题短小；若后紧跟标点再接长句则为正文
            if level == 1 and desc.startswith('第X卷/部'):
                if len(stripped) > 20:
                    continue  # 过长，视为正文
                if re.match(r'^.{2,}[，。、；：！？,.!?;:]', stripped):
                    continue  # 标题词后紧跟标点，视为正文

            # 二级标题（第X章含标题）额外过滤：排除正文中以"第X章"开头的评论/长句
            # 真正章节标题格式：第X章 + 空格 + 简短标题名；误匹配：第X章后跟标点+长句
            if level == 2 and desc == '第X章（含标题）':
                # 取出"第X章"之后的部分
                after_match = re.search(r'第[零一二两三四五六七八九十百千万0-9]+章(.*)', stripped)
                if after_match:
                    after = after_match.group(1)
                    if after and re.match(r'^\s*[，。、；：！？,.!?;:]', after):
                        continue  # 章号后紧跟标点，为正文评论
                    if len(stripped) > 50:
                        continue  # 过长，非章节标题

            return True, level, desc

    return False, None, None


def convert_txt_to_md(input_path, output_path, dry_run=False, verbose=False):
    """
    将TXT文件转换为Markdown格式。

    Args:
        input_path: 输入TXT文件路径（Path对象）
        output_path: 输出MD文件路径（Path对象）
        dry_run: 预览模式，不实际写入文件
        verbose: 详细输出模式

    Returns:
        bool: 转换是否成功
    """
    # 1. 读取文件
    encoding, lines = detect_encoding(input_path)
    if encoding is None:
        print("[ERROR] 无法解码文件，请尝试手动指定编码。")
        return False

    if not lines:
        print("[WARN] 文件为空，无内容可转换。")
        return False

    md_lines = []
    chapter_count = 0
    line_num = 0

    # 2. 处理第一行：设为一级标题（书名）
    first_line = lines[0].strip()
    if first_line:
        md_lines.append(f"# {first_line}\n\n")
        if verbose:
            print(f"[INFO] 书名标题: {first_line}")
        line_num = 1
    else:
        # 第一行是空行，跳过
        line_num = 1
        while line_num < len(lines) and not lines[line_num].strip():
            line_num += 1
        if line_num < len(lines):
            first_line = lines[line_num].strip()
            md_lines.append(f"# {first_line}\n\n")
            if verbose:
                print(f"[INFO] 书名标题（跳过前方空行）: {first_line}")
            line_num += 1

    # 3. 逐行处理正文
    prev_blank = False  # 上一行是否是空行（用于控制段落分段）

    while line_num < len(lines):
        raw_line = lines[line_num]
        stripped = raw_line.strip()
        line_num += 1

        # 空行：跳过，但标记为需要分段
        if not stripped:
            prev_blank = True
            continue

        # 检查是否为章节标题
        is_chapter, level, rule_desc = is_chapter_line(stripped)

        if is_chapter:
            chapter_count += 1
            # 章节标题：添加空行 + Markdown标题 + 空行
            md_lines.append(f"\n{'#' * level} {stripped}\n\n")
            if verbose:
                print(f"[CHAPTER] L{line_num}: {'#' * level} {stripped}  ({rule_desc})")
            prev_blank = False
        else:
            # 普通正文段落
            # 如果上一行是空行（新段落开始），直接添加内容
            # 否则（同一段落内）需要判断：txt中同一段落可能跨多行
            # 保守策略：只在明确空行分隔时才分段
            if prev_blank and md_lines and not md_lines[-1].endswith('\n\n'):
                # 上一个段落已结束（有空行间隔），当前行是新段落
                # 确保上一个段落末尾有 \n\n
                if md_lines[-1].endswith('\n'):
                    # 再加一个 \n 变成 \n\n
                    if not md_lines[-1].endswith('\n\n'):
                        md_lines.append('\n')
                md_lines.append(f"{stripped}\n\n")
            else:
                # 同一段落内，或文件开头
                md_lines.append(f"{stripped}\n\n")
            prev_blank = False

    # 4. 写入文件或预览
    md_content = ''.join(md_lines)

    if dry_run:
        print(f"\n[DRY-RUN] 转换预览（前2000字符）：")
        print("-" * 60)
        print(md_content[:2000])
        if len(md_content) > 2000:
            print(f"... (共 {len(md_content)} 字符，仅显示前2000)")
        print("-" * 60)
        print(f"[DRY-RUN] 检测到章节标题共 {chapter_count} 个")
        return True

    # 实际写入
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(md_content)
        print(f"[OK] 转换成功！输出文件: {output_path}")
        print(f"[OK] 共处理 {len(lines)} 行，识别章节标题 {chapter_count} 个")
        return True
    except Exception as e:
        print(f"[ERROR] 写入文件失败: {e}")
        return False


def check_chapter_continuity(md_file_path, verbose=False):
    """
    检查Markdown文件中章节标题的连续性。
    重点检查：序号是否连续、是否有跳号、是否有重复。

    Args:
        md_file_path: Markdown文件路径（Path对象）
        verbose: 详细输出，打印所有章节标题

    Returns:
        dict: 检查结果 {'issues': [...], 'chapters': [...]}
    """
    print(f"\n{'=' * 60}")
    print("章节连续性检查")
    print(f"{'=' * 60}")

    try:
        with open(md_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"[ERROR] 无法读取文件: {e}")
        return None

    # 提取所有标题行
    heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
    headings = heading_pattern.findall(content)

    if not headings:
        print("[WARN] 未检测到任何Markdown标题")
        return None

    print(f"共检测到 {len(headings)} 个标题（含所有级别）\n")

    if verbose:
        print("--- 所有标题列表 ---")
        for i, (level, title) in enumerate(headings):
            print(f"  {i+1:3d}. {level} {title}")
        print("--- 结束 ---\n")

    # 按级别分组，重点检查二级标题（章）的连续性
    level2_headings = [(i, title) for i, (level, title) in enumerate(headings) if len(level) == 2]

    if not level2_headings:
        print("[INFO] 未检测到二级标题（##），跳过连续性检查")
        return {'issues': [], 'chapters': headings}

    issues = []

    print(f"二级标题（##）共 {len(level2_headings)} 个，开始检查连续性...\n")

    # 提取序号：支持"第X章"、"Chapter X"、"第X节"等格式
    for idx_in_all, title in level2_headings:
        # 尝试提取数字序号
        num = extract_chapter_number(title)
        if num is not None:
            pass  # 有编号，后面检查连续性
        else:
            # 无编号标题（如"尾声"、"附录"），记录但不检查连续性
            if verbose:
                print(f"  [SKIP] 无编号标题: {title}")

    # 检查编号连续性（只考虑有编号的标题）
    numbered = []
    for idx_in_all, title in level2_headings:
        num = extract_chapter_number(title)
        if num is not None:
            numbered.append((idx_in_all, title, num))

    if len(numbered) < 2:
        print("[INFO] 有编号的章节不足2个，无需检查连续性")
    else:
        prev_num = numbered[0][2]
        prev_title = numbered[0][1]
        for idx_in_all, title, num in numbered[1:]:
            if num != prev_num + 1:
                gap = num - prev_num - 1
                issue = {
                    'type': 'gap',
                    'prev': f"第{prev_num}章 ({prev_title})",
                    'curr': f"第{num}章 ({title})",
                    'gap': gap,
                    'line_approx': idx_in_all + 1  # 近似行号
                }
                issues.append(issue)
                print(f"  [GAP] 章节不连续: 第{prev_num}章 → 第{num}章，跳过了 {gap} 章")
                print(f"        位置: 约第{idx_in_all + 1}行，标题: {title}")
            else:
                if verbose:
                    print(f"  [OK] 第{prev_num}章 → 第{num}章 ✓")
            prev_num = num
            prev_title = title

    # 检查重复章节号
    num_counts = {}
    for idx_in_all, title, num in numbered:
        if num not in num_counts:
            num_counts[num] = []
        num_counts[num].append((idx_in_all, title))

    duplicates = {k: v for k, v in num_counts.items() if len(v) > 1}
    if duplicates:
        print(f"\n[WARN] 检测到重复章节号:")
        for num, occurrences in duplicates.items():
            issues.append({'type': 'duplicate', 'num': num, 'occurrences': occurrences})
            for idx_in_all, title in occurrences:
                print(f"  [DUP] 第{num}章 出现多次: {title} (约第{idx_in_all+1}行)")

    # 汇总
    print(f"\n{'=' * 60}")
    print("检查汇总")
    print(f"{'=' * 60}")
    if not issues:
        print("[PASS] 章节连续性检查通过！未发现异常。")
    else:
        print(f"[FAIL] 发现 {len(issues)} 处问题:")
        for issue in issues:
            if issue['type'] == 'gap':
                print(f"  - 跳号: {issue['prev']} → {issue['curr']}（跳过{issue['gap']}章）")
            elif issue['type'] == 'duplicate':
                print(f"  - 重复: 第{issue['num']}章 出现 {len(issue['occurrences'])} 次")
    print(f"{'=' * 60}\n")

    return {'issues': issues, 'chapters': headings, 'numbered': numbered}


def extract_chapter_number(title):
    """
    从章节标题中提取数字序号。
    支持格式：
      - 第三里河章 → 3（中文数字）
      - 第123章 → 123（阿拉伯数字）
      - Chapter 5 → 5（英文）
      - 第3节 → 3
    返回 int 或 None（无法提取时）。
    """
    # 第X章 / 第X节（阿拉伯数字）
    m = re.search(r'第\s*([0-9]+)\s*[章章节]', title)
    if m:
        return int(m.group(1))

    # 第X章 / 第X节（中文数字）
    m = re.search(r'第\s*([零一二两三四五六七八九十百千万]+)\s*[章章节]', title)
    if m:
        return chinese_num_to_int(m.group(1))

    # Chapter X
    m = re.search(r'Chapter\s+([0-9]+)', title, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # 第X卷
    m = re.search(r'第\s*([0-9]+)\s*卷', title)
    if m:
        return int(m.group(1))

    m = re.search(r'第\s*([一二三四五六七八九十百千万]+)\s*卷', title)
    if m:
        return chinese_num_to_int(m.group(1))

    return None


def chinese_num_to_int(s):
    """
    将中文数字字符串转为int。
    支持：零~万（改进实现，覆盖常见情况）。
    例：十二=12，二十=20，一百零五=105，三千零二十=3020，二万一千=21000
    """
    num_map = {
        '零': 0, '〇': 0,
        '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
        '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '两': 2,
        '百': 100, '千': 1000, '万': 10000,
    }
    result = 0
    temp = 0
    for ch in s:
        if ch not in num_map:
            continue  # 跳过未知字符
        val = num_map[ch]
        if val == 0:  # 零/〇：占位符，不操作
            continue
        elif val < 10:  # 个位数
            temp += val
        else:  # 十、百、千、万
            if temp == 0:
                temp = 1
            result += temp * val
            temp = 0
    result += temp
    return result


def main():
    """CLI入口函数"""
    # Windows 控制台中文支持：尝试将 stdout/stderr 设为 utf-8
    try:
        import sys
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="TXT转Markdown工具 —— 将TXT小说/书籍转换为Markdown格式，自动识别章节标题",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 基本用法：转换TXT文件
    python txt_to_markdown.py "D:\\Books\\novel.txt"

    # 指定输出路径
    python txt_to_markdown.py "D:\\Books\\novel.txt" -o "D:\\Output\\novel.md"

    # 预览模式：查看转换结果但不写入文件
    python txt_to_markdown.py "D:\\Books\\novel.txt" --dry-run

    # 详细模式：打印每个识别到的章节标题
    python txt_to_markdown.py "D:\\Books\\novel.txt" -v

    # 跳过连续性检查
    python txt_to_markdown.py "D:\\Books\\novel.txt" --no-check
        """
    )

    # 必需参数
    parser.add_argument(
        "input",
        help="输入TXT文件路径"
    )

    # 可选参数
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="输出Markdown文件路径（默认为输入文件同名.md）"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式：显示转换结果但不写入文件"
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="详细输出：打印每个识别到的章节标题"
    )

    parser.add_argument(
        "--no-check",
        action="store_true",
        help="跳过转换后的章节连续性检查"
    )

    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="静默模式：只输出错误信息"
    )

    args = parser.parse_args()

    # 参数验证
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[ERROR] 文件不存在: {args.input}")
        sys.exit(1)
    if not input_path.is_file():
        print(f"[ERROR] 不是文件: {args.input}")
        sys.exit(1)

    # 确定输出路径
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix('.md')

    if not args.quiet:
        print(f"输入: {input_path}")
        print(f"输出: {output_path}")
        if args.dry_run:
            print("[INFO] 预览模式，不会写入文件")
        print()

    try:
        # 执行转换
        success = convert_txt_to_md(
            input_path, output_path,
            dry_run=args.dry_run,
            verbose=args.verbose
        )

        if not success:
            print("[ERROR] 转换失败")
            sys.exit(1)

        # 章节连续性检查（仅在实际写入文件后执行）
        if not args.dry_run and not args.no_check and success:
            check_result = check_chapter_continuity(output_path, verbose=args.verbose)
            if check_result and check_result['issues']:
                print("[WARN] 章节连续性检查发现问题，请检查上方报告")
                # 不因为检查问题而exit(1)，只是警告

    except KeyboardInterrupt:
        print("\n[INFO] 操作被用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"[ERROR] 处理失败: {str(e)}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
