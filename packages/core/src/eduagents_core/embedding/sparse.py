"""BM25 风格稀疏编码 (本地, 零 API)。

jieba 分词 (中文必须分词, §14) + xxhash.xxh32 词项哈希 -> {index: weight}。
"""

from __future__ import annotations

import jieba
import xxhash

# 内置常见中文停用词 (50 个): 高频虚词/代词/语气词, 对检索几乎无区分度。
_STOPWORDS = frozenset(
    {
        "的", "了", "是", "在", "和", "有", "就", "不", "都", "而",
        "及", "与", "或", "也", "很", "到", "要", "会", "着", "之",
        "于", "其", "被", "把", "对", "从", "但", "并", "等", "这",
        "那", "你", "我", "他", "她", "它", "们", "吗", "呢", "吧",
        "啊", "一个", "我们", "你们", "他们", "以及", "因为", "所以", "如果", "没有",
    }
)


class Bm25SparseEncoder:
    """BM25 文档/查询端稀疏权重编码器。

    取舍说明: 标准 BM25 的 avgdl 需要全局语料统计, 这里没有全局语料,
    取常量 AVG_DOC_LEN=512 作为本地近似 (与 fastembed 的 BM25 实现同思路)。
    IDF 同样依赖全局 DF, 留给检索端 (Qdrant 稀疏检索/RRF 融合) 处理,
    文档端只编码 tf 饱和 + 长度归一, 查询端每个去重词项权重恒为 1.0。

    词项索引用 xxhash.xxh32_intdigest(token): 稳定哈希, 跨进程/跨调用一致,
    文档端与查询端同 token 必然映射到同一索引。
    """

    K1 = 1.5
    B = 0.75
    AVG_DOC_LEN = 512.0

    def encode_document(self, text: str) -> dict[int, float]:
        """文档端: w = tf*(k1+1) / (tf + k1*(1 - b + b*doc_len/AVG_DOC_LEN))。"""
        tokens = self._tokenize(text)
        if not tokens:
            return {}
        tf: dict[int, int] = {}
        for token in tokens:
            idx = xxhash.xxh32_intdigest(token)
            tf[idx] = tf.get(idx, 0) + 1
        length_norm = self.K1 * (1.0 - self.B + self.B * len(tokens) / self.AVG_DOC_LEN)
        return {
            idx: freq * (self.K1 + 1.0) / (freq + length_norm) for idx, freq in tf.items()
        }

    def encode_query(self, text: str) -> dict[int, float]:
        """查询端: 每个去重 token 权重 1.0。"""
        return {xxhash.xxh32_intdigest(token): 1.0 for token in self._tokenize(text)}

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """jieba.lcut 分词, 过滤纯空白/单字符标点/停用词; 英文统一小写保证哈希稳定。"""
        tokens: list[str] = []
        for token in jieba.lcut(text):
            token = token.strip().lower()
            if not token:
                continue
            if len(token) == 1 and not token.isalnum():
                continue  # 单字符标点
            if token in _STOPWORDS:
                continue
            tokens.append(token)
        return tokens
