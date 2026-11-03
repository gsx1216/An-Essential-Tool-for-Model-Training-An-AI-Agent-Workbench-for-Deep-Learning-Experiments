"""纯文本切分：保留可识别段落，超长页在段落/句末附近切开。"""
import re
import unicodedata

TARGET_CHARACTERS = 4000
MAX_CHARACTERS = 6000
OVERLAP_CHARACTERS = 600
SPLITTER_VERSION = 'paragraph-v2'


def normalize_page(text):
    """保留提取器提供的空行段落；段内换行和空格统一，位置基于该文本。"""
    text = unicodedata.normalize('NFC', text).replace('\r\n', '\n').replace('\r', '\n')
    return '\n\n'.join(part for raw in re.split(r'\n[ \t]*\n+', text)
                       if (part := ' '.join(raw.split())))


def page_chunks(text):
    """返回 (起点, 终点)，每页独立；overlap 是上限而不是强制重复字数。"""
    paragraphs = [match.end() for match in re.finditer(r'\n\n', text)]
    sentences = [match.end() for match in re.finditer(r'[.!?。！？](?:["”’\']?)(?:\s+|$)', text)]
    start = 0
    while start < len(text):
        hard_end = min(start + MAX_CHARACTERS, len(text))
        end = hard_end
        if hard_end < len(text):
            # 优先完整段落；过长段落再退到句末，最后才按字符限制切断。
            for boundaries in (paragraphs, sentences):
                candidates = [p for p in boundaries if start + TARGET_CHARACTERS // 2 <= p <= hard_end]
                if candidates:
                    end = min(candidates, key=lambda p: (abs(p - start - TARGET_CHARACTERS), -p))
                    break
        yield start, end
        if end == len(text):
            break
        lower = max(start + 1, end - OVERLAP_CHARACTERS)
        candidates = [p for p in paragraphs if lower <= p < end]
        if not candidates:
            candidates = [p for p in sentences if lower <= p < end]
        if not candidates:
            candidates = [m.end() for m in re.finditer(r'\s+', text, flags=0) if lower <= m.end() < end]
        start = min(candidates) if candidates else lower
