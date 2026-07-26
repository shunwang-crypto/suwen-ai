import ctypes
import os
import sys
import tempfile

# conda 环境的 libstdc++ 必须先于系统版本加载 (pymupdf 触发系统版, qdrant 的
# sqlite3/icu 需要 conda 新 ABI, 顺序错了报 CXXABI_1.3.15 not found)。
_libstdcxx = os.path.join(sys.prefix, "lib", "libstdc++.so.6")
if os.path.exists(_libstdcxx):
    ctypes.CDLL(_libstdcxx, mode=ctypes.RTLD_GLOBAL)

# tests 统一走高限流阈值, 避免 auth 5次/分钟在测试中误触发
os.environ.setdefault("AUTH_RATE_LIMIT_PER_MINUTE", "10000")
os.environ.setdefault("API_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.setdefault("GATEWAY_MODE", "stub")
os.environ.setdefault("EMBEDDING_MODE", "stub")
os.environ.setdefault("RERANK_MODE", "stub")

# 集成测试的向量库/对象存储走独立临时目录 (Qdrant 本地模式 + LocalStorage)
_tmp_root = tempfile.mkdtemp(prefix="eduagents-test-")
os.environ.setdefault("QDRANT_LOCAL_PATH", os.path.join(_tmp_root, "qdrant"))
os.environ.setdefault("LOCAL_STORAGE_DIR", os.path.join(_tmp_root, "objects"))

sys.path.insert(0, os.path.dirname(__file__))
