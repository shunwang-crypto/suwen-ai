import os
import sys

# tests 统一走高限流阈值, 避免 auth 5次/分钟在测试中误触发
os.environ.setdefault("AUTH_RATE_LIMIT_PER_MINUTE", "10000")
os.environ.setdefault("API_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.setdefault("GATEWAY_MODE", "stub")

sys.path.insert(0, os.path.dirname(__file__))
