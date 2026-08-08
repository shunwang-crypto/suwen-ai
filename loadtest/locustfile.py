"""EduAgents SSE 压测画像 (M5)。

运行 (默认 --host http://127.0.0.1:8001, 即专供压测的 stub 档实例; 8000 上的 replay 实例不要动):

    locust -f loadtest/locustfile.py --headless -u 30 -r 5 -t 90s \
        --host http://127.0.0.1:8001 --csv docs/evidence/m5/loadtest_stub --html ... --only-summary

或直接 `bash scripts/run_loadtest.sh` (带健康检查与 gateway_mode 断言)。
账号池路径可用环境变量 LOADTEST_ACCOUNTS 覆盖, 默认 loadtest/accounts.json。

================================================================================
唯一决定: 预置账号池 + 预签 token, on_start 不做注册/登录
================================================================================
① auth 限流是 5 次/分钟/IP (auth_rate_limit_per_minute=5, register 与 login 共用),
   压测机对 API 而言就是一个 IP。30 并发在 on_start 里登录, 第 6 个用户起全是 429 —
   最终测出来的只是限流器的行为, 而不是问答链路的性能。
② 现场注册出来的新账号没有知识库、没有文档, 无法执行占 70% 权重的知识库问答任务
   (kb_id 为空就走不进 qa 子图), 主任务直接空转。
③ 压测目标画像是"已登录学生持续问答", 注册写库(bcrypt + INSERT)不属于被测画像,
   混进来只会污染 p95。

因此账号由 scripts/seed_loadtest.py 预先种好 (账号 + 知识库 + 已摄取语料 + 预签 token),
on_start 只做取模分配, 零 HTTP 开销。兜底: 若业务请求返回 401 (token 过期/密钥轮换),
则降级为一次真实 login 重取 token —— 这是异常路径的兜底, 不是常规路径; 并用
_relogin_done 标志限制每个 user 最多重登一次, 避免 30 个 user 同时撞 auth 限流。

================================================================================
为什么用 HttpUser 而不是 FastHttpUser
================================================================================
TTFE/TTFT 必须手动计时: 需要 requests 的 `stream=True` + `iter_lines()` 逐行读,
在读到第一个 `event:` 行、第一个 `event: token` 行的瞬间打点。FastHttpUser (geventhttpclient)
的流式接口不是 requests 语义, 逐行解码要自己拼 buffer, 反而更容易在计时上引入误差。
吞吐上限不是本次压测的瓶颈目标, 所以选可控性更高的 HttpUser。

================================================================================
TTFE / TTFT 定义 (两者显著不同, 必须分开统计)
================================================================================
带 kb_id 的 /api/chat 走 qa 子图, SSE 事件顺序是:
    node_update / trace_event / citation ... (若干过程事件) -> token x N (每 24 字符一块) -> done
- TTFE = 收到第一个 `event:` 行的时刻 - 请求发出时刻   → 首个过程事件, 反映"页面开始有反馈"
- TTFT = 收到第一个 `event: token` 行的时刻 - 请求发出 → 首个答案 token, 反映"开始看到答案"
locust 原生的响应时间统计只到响应头返回为止, 在 SSE 长连接下毫无意义 (恒等于握手耗时),
所以三个指标全部用 events.request.fire 自定义上报: chat_ttfe / chat_ttft / chat_stream_total。
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections import Counter
from pathlib import Path

from locust import HttpUser, between, events, task

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[1]
ACCOUNTS_PATH = Path(os.environ.get("LOADTEST_ACCOUNTS", str(REPO / "loadtest" / "accounts.json")))

# 问法风格对齐 eval/golden_set/v1.jsonl (高等数学, 中文疑问句, 覆盖极限/导数/中值定理/泰勒/积分)
QUESTIONS = [
    "数列极限的 ε-N 定义是什么?",
    "两个重要极限分别是什么?",
    "x 趋于 0 时 1 - cos x 的等价无穷小是什么?",
    "导数的几何意义是什么?",
    "复合函数求导的链式法则怎么用?",
    "罗尔定理的三个条件和结论是什么?",
    "拉格朗日中值定理的结论公式是什么?",
    "洛必达法则适用于哪些类型的未定式?",
    "泰勒公式的拉格朗日余项是什么形式?",
    "牛顿-莱布尼茨公式的内容是什么?",
]

# 429 计数: 压测实例本应把限流阈值调到极高 (见 scripts/run_loadtest.sh 里的启动命令),
# 一旦出现 429 说明环境没按口径起, 数据作废, 所以单独计数并打日志。
RATE_LIMITED: Counter[str] = Counter()

_accounts: list[dict] = []
_load_lock = threading.Lock()
_index_lock = threading.Lock()
_next_index = 0


def _load_accounts() -> list[dict]:
    if not ACCOUNTS_PATH.exists():
        raise RuntimeError(
            f"账号池不存在: {ACCOUNTS_PATH}\n"
            f"先执行: python scripts/seed_loadtest.py --count 10 --out loadtest/accounts.json"
        )
    payload = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    accounts = payload.get("accounts") or []
    if not accounts:
        raise RuntimeError(f"账号池为空: {ACCOUNTS_PATH}")
    return accounts


def _ensure_accounts() -> list[dict]:
    """懒加载: 放在 import 期会让 `locust -f ... --help` 在没种子时直接炸掉。"""
    global _accounts
    with _load_lock:
        if not _accounts:
            _accounts = _load_accounts()
    return _accounts


def _take_account() -> dict:
    """按全局自增索引取模分配账号: user 数 > 账号数时多个 user 共用一个账号 (api 限流是
    按 user_id 的 120 次/分钟, 压测实例已调高到 100000, 共用不会撞限流)。
    全局索引用锁保护 —— gevent 下切换点在 I/O, 但 on_start 里读写共享计数仍显式加锁更稳。"""
    global _next_index
    accounts = _ensure_accounts()
    with _index_lock:
        account = accounts[_next_index % len(accounts)]
        _next_index += 1
    return account


def _fire(name: str, response_time_ms: float, response_length: int = 0, exception=None) -> None:
    """统一的自定义指标上报入口 (request_type 固定 SSE, 便于在 csv 里一眼分辨)。"""
    events.request.fire(
        request_type="SSE",
        name=name,
        response_time=response_time_ms,
        response_length=response_length,
        exception=exception,
        context={},
        response=None,
    )


class EduAgentsUser(HttpUser):
    """已登录学生画像: 70% 知识库问答(SSE) / 15% 会话列表 / 10% 画像 / 5% 复习队列。"""

    wait_time = between(1, 3)

    def on_start(self) -> None:
        account = _take_account()
        self.email: str = account["email"]
        self.password: str = account["password"]
        self.kb_id: str = account["kb_id"]
        self.token: str = account["access_token"]
        self._relogin_done = False

    # ------------------------------------------------------------------ 工具
    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _relogin(self) -> bool:
        """401 兜底: 预签 token 失效 (TTL 900s 跑完了 / JWT_SECRET 变了) 时真实登录一次。
        每个 user 只重登一次 —— auth 限流 5 次/分钟/IP, 大规模重登会把自己打成 429。"""
        if self._relogin_done:
            return False
        self._relogin_done = True
        with self.client.post(
            "/api/auth/login",
            json={"email": self.email, "password": self.password},
            name="POST /api/auth/login (401 兜底)",
            catch_response=True,
            timeout=(5, 30),
        ) as resp:
            if resp.status_code == 200:
                self.token = resp.json()["access_token"]
                resp.success()
                logger.warning("%s: 预签 token 失效, 已重新登录", self.email)
                return True
            resp.failure(f"兜底重登失败 HTTP {resp.status_code}")
            logger.error("%s: 兜底重登失败 HTTP %s", self.email, resp.status_code)
            return False

    def _read_only_get(self, path: str, name: str) -> None:
        with self.client.get(
            path, headers=self._headers, name=name, catch_response=True, timeout=(5, 30)
        ) as resp:
            if resp.status_code == 429:
                RATE_LIMITED[name] += 1
                retry_after = resp.headers.get("Retry-After", "?")
                resp.failure(f"429 限流 (Retry-After={retry_after})")
                logger.warning(
                    "429 %s (累计 %d 次) — 压测实例限流阈值没调高, 数据不可用",
                    name,
                    RATE_LIMITED[name],
                )
            elif resp.status_code == 401:
                self._relogin()
                resp.failure("401 token 失效 (已触发兜底重登)")
            elif resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
            else:
                resp.success()

    # ------------------------------------------------------------------ 任务
    @task(70)
    def kb_chat_sse(self) -> None:
        """知识库问答 SSE: 手动计时 TTFE / TTFT / 整流耗时, 并自定义上报。

        每次都不带 session_id (即新建会话): 复用同一 session 会让服务端把历史消息不断
        拼进 prompt, 请求体与上下文长度随压测时长单调增长, payload 漂移会让 p95 越跑越差,
        测出来的是"历史累积效应"而不是稳态问答性能。
        """
        payload = {
            "message": random.choice(QUESTIONS),
            "kb_ids": [self.kb_id],
            "session_id": None,
        }
        t0 = time.perf_counter()
        ttfe_ms: float | None = None
        ttft_ms: float | None = None
        total_bytes = 0
        saw_done = False
        saw_error = False

        try:
            # timeout=(连接 5s, 两次读之间 120s): SSE 长连接不能用单一 total timeout
            with self.client.post(
                "/api/chat",
                json=payload,
                headers=self._headers,
                stream=True,
                catch_response=True,
                name="POST /api/chat (SSE)",
                timeout=(5, 120),
            ) as resp:
                if resp.status_code == 429:
                    RATE_LIMITED["POST /api/chat (SSE)"] += 1
                    resp.failure(f"429 限流 (Retry-After={resp.headers.get('Retry-After', '?')})")
                    logger.warning("429 /api/chat (累计 %d 次)", RATE_LIMITED["POST /api/chat (SSE)"])
                    return
                if resp.status_code == 401:
                    self._relogin()
                    resp.failure("401 token 失效 (已触发兜底重登)")
                    return
                if resp.status_code != 200:
                    resp.failure(f"HTTP {resp.status_code}")
                    return

                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue  # SSE 事件之间的空行
                    total_bytes += len(line.encode("utf-8")) + 1
                    if not line.startswith("event:"):
                        continue
                    if ttfe_ms is None:
                        # 首个过程事件 (node_update / trace_event / citation)
                        ttfe_ms = (time.perf_counter() - t0) * 1000
                        _fire("chat_ttfe", ttfe_ms)
                    event_name = line.split(":", 1)[1].strip()
                    if event_name == "token":
                        if ttft_ms is None:
                            # 首个答案 token (每 24 字符一块)
                            ttft_ms = (time.perf_counter() - t0) * 1000
                            _fire("chat_ttft", ttft_ms)
                    elif event_name == "done":
                        saw_done = True
                    elif event_name == "error":
                        saw_error = True

                total_ms = (time.perf_counter() - t0) * 1000
                _fire("chat_stream_total", total_ms, response_length=total_bytes)

                if saw_error:
                    resp.failure("SSE 返回 event: error")
                elif not saw_done:
                    resp.failure("流结束但未收到 event: done (断流)")
                elif ttft_ms is None:
                    resp.failure("收到 done 但全程无 event: token (空答案)")
                else:
                    resp.success()
        # 压测 user 绝不能因单次异常崩掉: 崩一个 user 就少一份并发, 后半程并发数会悄悄缩水
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            _fire("chat_stream_total", elapsed_ms, response_length=total_bytes, exception=exc)
            logger.warning("SSE 异常 (%.0fms): %s: %s", elapsed_ms, type(exc).__name__, exc)

    @task(15)
    def list_sessions(self) -> None:
        self._read_only_get("/api/chat/sessions", "GET /api/chat/sessions")

    @task(10)
    def get_profile(self) -> None:
        self._read_only_get("/api/profile", "GET /api/profile")

    @task(5)
    def reviews_today(self) -> None:
        self._read_only_get("/api/reviews/today", "GET /api/reviews/today")


@events.test_start.add_listener
def on_test_start(environment, **kwargs) -> None:
    accounts = _ensure_accounts()
    opts = environment.parsed_options
    users = getattr(opts, "num_users", None)
    spawn_rate = getattr(opts, "spawn_rate", None)
    run_time = getattr(opts, "run_time", None)
    logger.info("=" * 72)
    logger.info("EduAgents 压测启动")
    logger.info("  host        : %s", environment.host)
    logger.info("  账号池      : %s (%d 个账号)", ACCOUNTS_PATH, len(accounts))
    logger.info("  并发档位    : users=%s spawn_rate=%s run_time=%s", users, spawn_rate, run_time)
    logger.info("  任务权重    : chat_sse=70 sessions=15 profile=10 reviews=5")
    logger.info("  自定义指标  : chat_ttfe / chat_ttft / chat_stream_total (request_type=SSE)")
    logger.info("=" * 72)


@events.quitting.add_listener
def on_quitting(environment, **kwargs) -> None:
    """CSV/HTML 报告由命令行 --csv / --html 产出, 这里不做任何额外落盘。
    仅在出现过 429 时提醒一句: 那说明压测实例的限流阈值没按口径调高, 结果不可用。"""
    if RATE_LIMITED:
        logger.error("本轮出现 429 限流: %s — 数据不可用, 见 loadtest/README.md 启动口径", dict(RATE_LIMITED))
