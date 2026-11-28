import ctypes
import os
import shutil
import sys
import tempfile

import pytest

# conda 环境的 libstdc++ 必须先于系统版本加载 (pymupdf 触发系统版, qdrant 的
# sqlite3/icu 需要 conda 新 ABI, 顺序错了报 CXXABI_1.3.15 not found)。
_libstdcxx = os.path.join(sys.prefix, "lib", "libstdc++.so.6")
if os.path.exists(_libstdcxx):
    ctypes.CDLL(_libstdcxx, mode=ctypes.RTLD_GLOBAL)

# tests 统一走高限流阈值, 避免 auth 5次/分钟在测试中误触发
os.environ.setdefault("AUTH_RATE_LIMIT_PER_MINUTE", "10000")
os.environ.setdefault("API_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_FORGOT_RATE_LIMIT_PER_MINUTE", "10000")
os.environ.setdefault("CAPTCHA_RATE_LIMIT_PER_MINUTE", "100000")
# 注册验证码测试通道: 既有集成测试大量直接注册, 全局显式关闭;
# 验证码专项测试用 monkeypatch.setenv("CAPTCHA_REQUIRED_REGISTER", "1") 单独打开
os.environ.setdefault("CAPTCHA_REQUIRED_REGISTER", "0")
# Existing integration helpers intentionally exercise the pre-verification
# auth flows. Dedicated email-registration tests enable this explicitly.
os.environ.setdefault("EMAIL_VERIFICATION_REQUIRED", "0")
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ["GATEWAY_MODE"] = "stub"
os.environ["OLLAMA_CHAT_MODEL"] = ""
os.environ["OLLAMA_REQUIRE_GPU"] = "false"
os.environ["EMBEDDING_MODE"] = "stub"
os.environ["RERANK_MODE"] = "stub"
os.environ["RETRIEVAL_ENABLE_RERANK"] = "true"
os.environ["MEMORY_SEMANTIC_MODE"] = "disabled"
os.environ.setdefault("HARNESS_MAX_CONCURRENCY", "2")
os.environ.setdefault("AGENT_COLLABORATION_MAX_PARALLEL", "1")
# Existing regression fixtures pin their historical instruction contract;
# collaboration tests explicitly select edu-v2.
os.environ["EDUCATION_PROMPT_VERSION"] = "edu-v1"
# Integration tests must never inherit the developer's SMTP configuration.
# The password-reset suite uses DevEmailSender and reads its debug link from
# the isolated Redis instance instead of attempting network delivery.
for _smtp_key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_REPLY_TO"):
    os.environ[_smtp_key] = ""

# 集成测试的向量库/对象存储走独立临时目录 (Qdrant 本地模式 + LocalStorage)
_tmp_root = tempfile.mkdtemp(prefix="eduagents-test-")
TEST_RESOURCE_ROOT = _tmp_root
os.environ["QDRANT_LOCAL_PATH"] = os.path.join(_tmp_root, "qdrant")
os.environ["LOCAL_STORAGE_DIR"] = os.path.join(_tmp_root, "objects")

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_resources():
    """Remove only the per-process local Qdrant/object-storage directory."""
    yield
    shutil.rmtree(TEST_RESOURCE_ROOT, ignore_errors=True)
