"""KC 知识点体系 (§5.8): 受控词表 + 两遍式归一化 + 先修 DAG + LLM 抽取。"""

from .dag import add_edge_safe, topological_order, validate_acyclic
from .extraction import KcExtractionError, build_vocabulary, extract_chunk_kcs
from .normalizer import KcNormalizer, cosine_similarity
from .types import KcEntry, KcVocabulary, PrereqEdge, normalize_kc_name

__all__ = [
    "KcEntry",
    "KcExtractionError",
    "KcNormalizer",
    "KcVocabulary",
    "PrereqEdge",
    "add_edge_safe",
    "build_vocabulary",
    "cosine_similarity",
    "extract_chunk_kcs",
    "normalize_kc_name",
    "topological_order",
    "validate_acyclic",
]
