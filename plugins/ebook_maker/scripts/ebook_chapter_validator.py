#!/usr/bin/env python3
"""
电子书目录检查器（CLI版）

检查 Markdown 格式电子书的章节编号是否连续。支持中文数字和阿拉伯数字混合的章节标题，
例如：第一章、第一百零一章、第1章、第101章等。

用法:
    python ebook_chapter_validator.py <电子书路径>
    python ebook_chapter_validator.py <电子书路径> -o report.txt
    python ebook_chapter_validator.py <电子书路径> --verbose

示例:
    python ebook_chapter_validator.py D:/Books/小说.md
    python ebook_chapter_validator.py D:/Books/小说.md -o D:/Reports/检查报告.txt
    python ebook_chapter_validator.py D:/Books/小说.md --dry-run

依赖:
    无（仅使用 Python 标准库）
"""

import os
import sys
import argparse
import re
import time
import shutil
from pathlib import Path
from collections import Counter


# ============================================================
# 中文数字转换
# ============================================================

# 基础数字映射
_CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 位权映射
_CN_UNIT = {
    "十": 10,
    "百": 100,
    "千": 1000,
}


def chinese_to_arabic(cn_str: str) -> int:
    """
    中文数字字符串转阿拉伯数字。
    支持范围: 一 ~ 九千九百九十九。
    """
    if not cn_str:
        return 0

    # 纯数字（如 "101"）直接返回
    if cn_str.isdigit():
        return int(cn_str)

    # 去掉可能的前导"零"
    cn_str = cn_str.lstrip("零")
    if not cn_str:
        return 0

    total = 0
    current = 0
    has_unit = False

    for ch in cn_str:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            has_unit = True
            unit = _CN_UNIT[ch]
            if current == 0:
                # 十、百、千 前面省略了一
                current = 1
            total += current * unit
            current = 0
        else:
            raise ValueError(f"无法识别的中文字符: '{ch}'")

    total += current

    # 处理"零"在中间的情况：一百零一 → 已由上循环自然处理
    if not has_unit and cn_str in _CN_DIGIT:
        return _CN_DIGIT[cn_str]

    return total


# ============================================================
# 章节提取
# ============================================================

# 匹配 Markdown 标题中的章节模式
# 支持: # 第一章、## 第101章、### 第一百零一章 等
_CHAPTER_PATTERN = re.compile(
    r'^#{1,6}\s*第\s*([零一二三四五六七八九十百千\d]+)\s*章',
    re.MULTILINE
)

# 匹配所有 Markdown 标题行（用于检测未匹配第X章模式的标题）
_ALL_HEADING_PATTERN = re.compile(
    r'^(#{1,6})\s+(.+)$', re.MULTILINE
)


def _progress_bar(current, total, bar_len=40):
    """绘制简单进度条（标准库实现，无需 tqdm）。"""
    if total == 0:
        percent = 0
        filled = 0
    else:
        percent = current * 100 // total
        filled = current * bar_len // total

    bar = '█' * filled + '░' * (bar_len - filled)
    term_width = shutil.get_terminal_size().columns
    line = f'\r  [{bar}] {percent:3d}% | {current:,}/{total:,}'
    # 截断到终端宽度，避免溢出
    if len(line) > term_width - 1:
        line = line[:term_width - 1]
    sys.stdout.write(line)
    sys.stdout.flush()


def extract_chapters(file_path: Path, show_progress: bool = True) -> tuple[list[dict], str]:
    """
    从 Markdown 文件中提取所有章节信息。

    返回列表，每项包含:
        line_no : 行号 (1-based)
        raw     : 原始标题行
        number  : 解析后的阿拉伯数字章节号
    """
    chapters = []
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = file_path.read_text(encoding="gbk")

    if show_progress:
        file_size = file_path.stat().st_size
        print(f"\n  正在读取文件 (大小: {file_size / 1024:.1f} KB) ...")

    total_lines = content.count("\n") + 1

    for idx, match in enumerate(_CHAPTER_PATTERN.finditer(content)):
        cn_num = match.group(1)
        try:
            num = chinese_to_arabic(cn_num)
        except ValueError:
            continue  # 无法解析的跳过

        line_no = content[:match.start()].count("\n") + 1
        raw = match.group(0)
        chapters.append({
            "line_no": line_no,
            "raw": raw.strip(),
            "number": num,
        })

        if show_progress and total_lines > 1:
            _progress_bar(line_no, total_lines)

    if show_progress:
        _progress_bar(total_lines, total_lines)
        print()  # 换行

    return chapters, content


# ============================================================
# 连续性检查
# ============================================================

def check_continuity(chapters: list[dict]) -> dict:
    """
    检查章节编号是否连续。

    返回:
        total     : 章节总数
        min_num   : 最小章节号
        max_num   : 最大章节号
        missing   : 缺失的章节号列表
        duplicates: {章节号: [行号列表]} 重复章节
        all_ok    : 是否全部正常
    """
    if not chapters:
        return {
            "total": 0,
            "min_num": 0,
            "max_num": 0,
            "missing": [],
            "duplicates": {},
            "all_ok": True,
        }

    numbers = [ch["number"] for ch in chapters]
    counted = Counter(numbers)

    min_num = min(numbers)
    max_num = max(numbers)

    # 缺失的章节号
    expected = set(range(min_num, max_num + 1))
    actual = set(numbers)
    missing = sorted(expected - actual)

    # 重复的章节号
    duplicates = {}
    for num, cnt in counted.items():
        if cnt > 1:
            duplicates[num] = [
                ch["line_no"] for ch in chapters if ch["number"] == num
            ]

    all_ok = (len(missing) == 0 and len(duplicates) == 0)

    return {
        "total": len(chapters),
        "min_num": min_num,
        "max_num": max_num,
        "missing": missing,
        "duplicates": duplicates,
        "all_ok": all_ok,
    }


# ============================================================
# 未识别标题检测
# ============================================================

def find_unrecognized_headings(content: str, chapters: list[dict]) -> list[dict]:
    """
    检测未匹配第X章模式的标题行，根据前后已识别章节的连续性判断其是否可疑。

    返回列表，每项包含:
        line_no     : 行号 (1-based)
        raw         : 原始标题行
        level       : 标题级别（# 的数量）
        verdict     : "suspicious" | "info" | "edge"
        explanation : 说明文字
    """
    recognized_lines = {ch["line_no"] for ch in chapters}

    results = []
    for match in _ALL_HEADING_PATTERN.finditer(content):
        line_no = content[:match.start()].count("\n") + 1

        # 跳过已匹配第X章模式的标题
        if line_no in recognized_lines:
            continue

        raw = match.group(0).strip()
        level = len(match.group(1))

        # 找前后最近的已识别章节
        before = None
        after = None
        for ch in chapters:
            if ch["line_no"] < line_no:
                before = ch
            if ch["line_no"] > line_no and after is None:
                after = ch

        verdict = "info"
        parts = []

        if before:
            parts.append(f"前接第{before['number']}章")
        else:
            parts.append("开篇标题")

        if after:
            gap = after["number"] - before["number"] if before else 0
            parts.append(f"后接第{after['number']}章")
            if before and gap == 1:
                verdict = "suspicious"
                parts.append("前后章节连续 → 可能是写错的章节名")
            elif before:
                parts.append(f"间隔{gap}个编号，无法判断")
        else:
            parts.append("结尾标题")

        results.append({
            "line_no": line_no,
            "raw": raw,
            "level": level,
            "verdict": verdict,
            "explanation": "，".join(parts),
        })

    return results


# ============================================================
# 报告生成
# ============================================================

def generate_report(
    file_path: Path,
    chapters: list[dict],
    result: dict,
    unrecognized: list[dict] = None,
    verbose: bool = False,
) -> str:
    """生成可读的检查报告。"""
    lines = []
    fname = file_path.name

    lines.append("=" * 60)
    lines.append(f"  电子书目录检查报告")
    lines.append(f"  文件: {file_path}")
    lines.append("=" * 60)
    lines.append("")

    if result["total"] == 0:
        lines.append("  未检测到任何章节标题。")
        lines.append("  支持的格式: # 第X章 / ## 第X章 等")
        return "\n".join(lines)

    # 统计摘要
    lines.append(f"  共检测到 {result['total']} 个章节标题")
    lines.append(f"  章节范围: 第 {result['min_num']} 章 ~ 第 {result['max_num']} 章")
    lines.append("")

    if result["all_ok"]:
        lines.append("  ✓ 目录连续，未发现问题。")
    else:
        # 缺失章节
        if result["missing"]:
            lines.append(f"  ✗ 缺失章节 ({len(result['missing'])} 个):")
            lines.append("    " + ", ".join(f"第{n}章" for n in result["missing"]))
            lines.append("")

        # 重复章节
        if result["duplicates"]:
            lines.append(f"  ✗ 重复章节 ({len(result['duplicates'])} 处):")
            for num, line_nos in sorted(result["duplicates"].items()):
                lines.append(f"    第{num}章 出现在行: {', '.join(str(ln) for ln in line_nos)}")
            lines.append("")

    # 未识别标题
    if unrecognized:
        suspicious_count = sum(1 for u in unrecognized if u["verdict"] == "suspicious")
        total_unrec = len(unrecognized)
        lines.append(f"  未匹配「第X章」模式的标题: 共 {total_unrec} 个",)
        if suspicious_count > 0:
            lines.append(f"    其中 {suspicious_count} 个可疑（前后章节连续，可能是写错的章节名）")
        lines.append("")
        for u in unrecognized:
            marker = "⚠" if u["verdict"] == "suspicious" else "‧"
            prefix = u["raw"].split(maxsplit=1)[0] if u["raw"].startswith("#") else u["raw"]
            text = u["raw"].lstrip("#").strip()
            lines.append(f"    {marker} 行 {u['line_no']:>6d}: {prefix} {text}   ← {u['explanation']}")
        lines.append("")

    # 详细列表
    if verbose:
        lines.append("-" * 60)
        lines.append("  章节列表:")
        lines.append("")
        for ch in chapters:
            marker = ""
            if ch["number"] in result["missing"]:
                marker = "  ← 缺失"
            elif ch["number"] in result["duplicates"]:
                marker = "  ← 重复"
            lines.append(f"  行 {ch['line_no']:>6d}: 第{ch['number']:>4d}章  {ch['raw']}{marker}")
        lines.append("")

    return "\n".join(lines)


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="检查 Markdown 电子书章节编号是否连续",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python ebook_chapter_validator.py D:/Books/小说.md
    python ebook_chapter_validator.py D:/Books/小说.md -o report.txt
    python ebook_chapter_validator.py D:/Books/小说.md -v
    python ebook_chapter_validator.py D:/Books/小说.md --dry-run
        """,
    )

    parser.add_argument(
        "input",
        help="电子书 Markdown 文件路径",
    )

    parser.add_argument(
        "-o", "--output",
        default=None,
        help="将报告写入指定文件（默认打印到终端）",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="详细输出，列出每个章节及其行号",
    )

    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="静默模式，仅输出错误和缺失/重复信息",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式，仅显示将要检查的文件",
    )

    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="禁用进度条显示",
    )

    args = parser.parse_args()

    input_path = Path(args.input).resolve()

    # --dry-run
    if args.dry_run:
        print(f"预览: 将检查文件 {input_path}")
        if not input_path.exists():
            print(f"  注意: 文件当前不存在")
        return

    # 参数验证
    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)
    if not input_path.is_file():
        print(f"错误: 不是有效文件: {input_path}", file=sys.stderr)
        sys.exit(1)
    if input_path.suffix.lower() not in (".md", ".markdown", ".txt"):
        print(f"警告: 文件扩展名不是 .md，但仍尝试检查", file=sys.stderr)

    try:
        # 提取章节
        chapters, content = extract_chapters(input_path, show_progress=not args.no_progress)

        # 检查连续性
        result = check_continuity(chapters)

        # 检测未识别标题
        unrecognized = find_unrecognized_headings(content, chapters)

        # 生成报告
        report = generate_report(input_path, chapters, result, unrecognized, verbose=args.verbose)

        # 输出
        if args.output:
            out_path = Path(args.output).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report, encoding="utf-8")
            if not args.quiet:
                print(f"报告已写入: {out_path}")
        else:
            if args.quiet:
                # 静默模式仅输出关键信息
                if result["missing"]:
                    print(f"缺失: {', '.join(f'第{n}章' for n in result['missing'])}")
                if result["duplicates"]:
                    for num, line_nos in sorted(result["duplicates"].items()):
                        print(f"重复: 第{num}章 (行 {', '.join(str(ln) for ln in line_nos)})")
                if result["all_ok"]:
                    # 静默且无问题，不输出
                    pass
            else:
                print(report)

        # 退出码
        if not result["all_ok"]:
            sys.exit(1)

    except Exception as e:
        print(f"处理失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()