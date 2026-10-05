"""中文分词（jieba）——为 FTS5 产出「空格拼接的分词文本」。

DESIGN.md §6.1：
- title / description / source：jieba.posseg 分词，按词性只保留实词
  （名词 n*、动词 v*、形容词 a*、英文 eng），虚词（的/了/与/和）与单字丢弃
- tags：按 `||` 拆分，每段整体作为一个关键词，同时对段内文本再分词一并提出
  （用户搜「医疗」要能命中标签「医疗健康」，只留整段 token 会漏召回）
- category_id：英文编码，按 `||` 拆开后直接空格拼接，不再分词

产出字符串写入 product_fts 的对应列；查询侧（Phase 3）用同样的规则处理用户输入。
"""

from __future__ import annotations

import logging
import re
import warnings

log = logging.getLogger(__name__)

# 保留的词性前缀：名词 / 动词 / 形容词 / 英文（外来词、缩写如 AI、ASR）
KEEP_FLAGS: frozenset[str] = frozenset(
    {
        # 名词类
        "n", "nr", "ns", "nt", "nz", "ng", "nw", "nv",
        # 动词类
        "v", "vn", "vd", "vf", "vx", "vi", "vl", "vg",
        # 形容词类
        "a", "ad", "an", "ag", "al",
        # 英文 / 字母数字词
        "eng",
    }
)

TAG_SEPARATOR = "||"
_WS = re.compile(r"\s+")

_initialized = False


def initialize(verbose: bool = False) -> None:
    """预热 jieba 词典（首次调用耗时约 1s，放在 main 开头避免中途卡顿）。"""
    global _initialized
    if _initialized:
        return
    # jieba 源码里有若干非法转义序列，导入时会在 Python 3.12+ 刷一屏 SyntaxWarning，
    # 与我们无关，静默掉即可。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba
        import jieba.posseg as pseg  # noqa: F401

    if not verbose:
        jieba.setLogLevel(logging.ERROR)
    jieba.initialize()
    _initialized = True


def tokenize_text(text: str | None) -> str:
    """对一段自然语言分词并按词性过滤，返回空格拼接串。"""
    if not text:
        return ""
    initialize()
    import jieba.posseg as pseg

    kept: list[str] = []
    for word, flag in pseg.cut(text):
        word = word.strip()
        if len(word) < 2:  # 单字虚词/停用词
            continue
        if flag not in KEEP_FLAGS:
            continue
        kept.append(word)
    return " ".join(_dedupe(kept))


def tokenize_tags(tags: str | None) -> str:
    """tags 列专用：整段保留 + 段内再分词。

    例："医疗健康||人工智能" → "医疗健康 医疗 健康 人工智能 人工 智能"
    """
    if not tags:
        return ""
    kept: list[str] = []
    for segment in tags.split(TAG_SEPARATOR):
        segment = segment.strip()
        if not segment:
            continue
        # 1) 整段作为一个关键词，保证精确召回
        kept.append(_WS.sub(" ", segment))
        # 2) 段内再分词，保证部分召回（搜「医疗」命中「医疗健康」）
        inner = tokenize_text(segment)
        if inner:
            kept.append(inner)
    return " ".join(_dedupe(token for chunk in kept for token in chunk.split(" ")))


def tokenize_category_ids(category_id: str | None) -> str:
    """category_id 列专用：按 `||` 拆开后空格拼接即可。

    分类编码本身是英文单词/词组（如 ai_service），已经是最小语义单位，
    无需再分词；另外 unicode61 会把下划线当作分隔符，
    故 `ai_service` 会被拆成 `ai` `service` 两个 token ——
    查询侧同样处理，两边一致即可正确命中。
    """
    if not category_id:
        return ""
    return " ".join(
        _dedupe(segment.strip() for segment in category_id.split(TAG_SEPARATOR))
    )


def build_fts_row(
    *,
    title: str | None,
    description: str | None,
    source: str | None,
    tags: str | None,
    category_id: str | None = None,
) -> dict[str, str]:
    """生成 product_fts 一行所需的字段值。"""
    return {
        "title": tokenize_text(title),
        "description": tokenize_text(description),
        "source": tokenize_text(source),
        "tags": tokenize_tags(tags),
        "category_id": tokenize_category_ids(category_id),
    }


def _dedupe(tokens) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out
