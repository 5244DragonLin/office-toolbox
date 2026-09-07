"""中文数字 → 阿拉伯数字（ebook_maker 插件 scripts/ 的共享工具模块）。

此前本插件有三份行为各异的实现（txt_to_markdown / markdown_to_epub /
ebook_chapter_validator 各一份），其中两份对「二十一万」「一百万」这类
万级数字会算错。现统一为本模块的唯一实现，各脚本按需 import。
纯标准库，无第三方依赖。
"""
_DIGITS = {
    "零": 0, "〇": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_UNITS = {"十": 10, "百": 100, "千": 1000}


def chinese_to_arabic(cn_str: str) -> int:
    """中文数字字符串转阿拉伯数字，支持 零/〇/两、十百千 与 万，范围 0 ~ 9999_9999。

    例：十二=12，一百零五=105，二十一万=210000，一万零五=10005，第101章的"101"=101。
    无法识别的字符抛 ValueError。
    """
    s = (cn_str or "").strip()
    if not s:
        raise ValueError("空字符串不是有效的中文数字")
    if s.isdigit():
        return int(s)

    def _section(sec: str) -> int:
        """处理不含「万」的一段（≤9999）：十百千按位权累加。"""
        total, current = 0, 0
        for ch in sec:
            if ch in _DIGITS:
                current = _DIGITS[ch]
            elif ch in _UNITS:
                if current == 0:
                    current = 1  # 十、百、千 前面省略了一
                total += current * _UNITS[ch]
                current = 0
            else:
                raise ValueError(f"无法识别的中文数字字符: {ch!r}")
        return total + current

    if "万" in s:
        # 万按「万」切段：高位段 × 10000 + 低位段（低位段可再含十百千，不含万）
        high, _, low = s.partition("万")
        return _section(high) * 10000 + (_section(low) if low else 0)
    return _section(s)
