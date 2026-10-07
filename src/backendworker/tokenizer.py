"""中文分词（jieba）——为 FTS5 产出「空格拼接的分词文本」。

DESIGN.md §6.1：
- title / description / source：jieba.posseg 分词，按词性只保留实词
  （名词 n*、动词 v*、形容词 a*、简称 j、英文 eng），虚词（的/了/与/和）与单字丢弃
- tags：按 `||` 拆分，每段整体作为一个关键词，同时对段内文本再分词一并提出
  （用户搜「医疗」要能命中标签「医疗健康」，只留整段 token 会漏召回）
- category_id：英文编码，按 `||` 拆开后直接空格拼接，不再分词

产出字符串写入 product_fts 的对应列；查询侧（Phase 3）用同样的规则处理用户输入。

⚠️ **改了 KEEP_FLAGS 就必须重建 product_fts** —— 索引侧口径变了，
    旧索引不会自动跟着变，新词（如房地产/仓储）仍然搜不到。
    重建用：`DELETE FROM product_fts;` 后重跑导入（或单独写回填脚本）。
"""

from __future__ import annotations

import logging
import re
import warnings

log = logging.getLogger(__name__)

# 保留的词性前缀：名词 / 动词 / 形容词 / 英文（外来词、缩写如 AI、ASR）
#
# ⭐ 2026-10-07 新增 'j'（简称略语abbreviation）——
#    为什么需要：本项目的商品全是「行业/领域数据集」，而 jieba 词典把一批
#    领域固定搭配收进了**简称表**，标成 `j` 而非 `n`，
#    于是这些词切出来了却被 POS 过滤丢掉：
#        不动产→实测是 l；房地产 / 仓储 / 环保 / 社保 / 房产证 / 土地证 → j
#    后果是**索引里根本没有这些词**，站内搜索搜不到，
#    而且读端再怎么改都救不回来（读端拿不到不存在的 token）。
#    补上 'j' 后上面 7 个词全部进索引，实测零噪声（1396 个 j 词全是实义领域词）。
#
# ⚠️ **故意不加 'l'（习用语idiom）**：jieba 词典里 l 有 17721 个词，
#    随机抽样可见大量口语/成语 —— 毫不迟疑、满肚子火、跑上跑下、榜上有名、
#    懵懵、几时休……对商品检索毫无价值，全放进索引只会稀释倒排索引。
#    `不动产` 那个词可另想办法（见 DESIGN 说明），但代价远大于收益。
KEEP_FLAGS: frozenset[str] = frozenset(
    {
        # 名词类
        "n", "nr", "ns", "nt", "nz", "ng", "nw", "nv",
        # 动词类
        "v", "vn", "vd", "vf", "vx", "vi", "vl", "vg",
        # 形容词类
        "a", "ad", "an", "ag", "al",
        # 简称略语：房地产 / 仓储 / 环保 / 社保 / 房产证 / 土地证 …（详见上方说明）
        "j",
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
