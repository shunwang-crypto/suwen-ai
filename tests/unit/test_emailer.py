"""EmailSender 单测: 工厂选择 / DevEmailSender 的 dev 专属 debug 键 / SMTP 判定。

真实 SMTP 发送 (SMTPEmailSender._send_sync) 不在单测内联网; 这里验证
选择逻辑与 Dev 替身的诚实行为 (不伪造发送、token 不出 Redis debug 键之外)。
"""
import pytest
from eduagents_api.emailer import (
    DEBUG_RESET_KEY_PREFIX,
    DEBUG_RESET_TTL_SECONDS,
    DevEmailSender,
    SMTPEmailSender,
    create_email_sender,
    smtp_configured,
)
from eduagents_core.config import Settings
from fakeredis import FakeAsyncRedis

_SMTP_KW = dict(
    smtp_host="smtp.example.com",
    smtp_user="bot@example.com",
    smtp_password="secret",
    smtp_from="EduAgents <bot@example.com>",
)


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


def test_smtp_configured_requires_all_four():
    assert smtp_configured(Settings(_env_file=None, **_SMTP_KW)) is True
    for missing in ("smtp_host", "smtp_user", "smtp_password", "smtp_from"):
        kw = dict(_SMTP_KW)
        kw[missing] = ""
        assert smtp_configured(Settings(_env_file=None, **kw)) is False


def test_factory_selects_real_smtp_when_configured(redis):
    sender = create_email_sender(Settings(_env_file=None, **_SMTP_KW), redis)
    assert isinstance(sender, SMTPEmailSender)


def test_factory_falls_back_to_dev_sender(redis):
    sender = create_email_sender(Settings(_env_file=None), redis)
    assert isinstance(sender, DevEmailSender)


async def test_dev_sender_writes_debug_key_only_in_dev(redis):
    settings = Settings(_env_file=None, app_env="dev")
    sender = DevEmailSender(settings, redis)
    link = "http://localhost:3000/reset?token=abc123DEF"
    await sender.send("u@test.cn", "密码重置", f"点击链接:\n{link}\n30分钟内有效")
    stored = await redis.get(f"{DEBUG_RESET_KEY_PREFIX}u@test.cn")
    assert stored == link
    ttl = await redis.ttl(f"{DEBUG_RESET_KEY_PREFIX}u@test.cn")
    assert 0 < ttl <= DEBUG_RESET_TTL_SECONDS


@pytest.mark.parametrize("env", ["test", "prod"])
async def test_dev_sender_no_debug_key_outside_dev(redis, env):
    """非 dev 环境连 debug 键都不写: token 只存在于日志 (且明确标注未发信)。"""
    settings = Settings(_env_file=None, app_env=env)
    sender = DevEmailSender(settings, redis)
    await sender.send(
        "u@test.cn", "密码重置", "http://localhost:3000/reset?token=abc123DEF"
    )
    assert await redis.get(f"{DEBUG_RESET_KEY_PREFIX}u@test.cn") is None


async def test_dev_sender_body_without_link_writes_nothing(redis):
    sender = DevEmailSender(Settings(_env_file=None, app_env="dev"), redis)
    await sender.send("u@test.cn", "无链接邮件", "正文里没有 URL")
    assert await redis.keys(f"{DEBUG_RESET_KEY_PREFIX}*") == []
