"""邮件发送 (密码找回链路)。

诚实纪律 (零虚假实现):
- SMTPEmailSender: **真实 SMTP 发送** (smtplib, 465 走 SSL 直连, 其他端口 STARTTLS),
  仅当 smtp_host/user/password/from 四项齐全时由工厂启用;
- DevEmailSender: SMTP 未配置时的替身 —— structlog 明确打日志「开发模式未真实发信」,
  绝不伪造"邮件已发送"的假象; 仅当 APP_ENV=dev 时把重置链接写入
  Redis ``debug:reset:{email}`` (TTL 10min) 供本地演示取用,
  **token 永远不进 HTTP 响应**。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import urlparse

from eduagents_core.config import Settings
from eduagents_core.logging import get_logger

from .email_utils import normalize_email

log = get_logger("emailer")

DEBUG_RESET_KEY_PREFIX = "debug:reset:"
DEBUG_RESET_TTL_SECONDS = 600  # dev 演示链接保留 10min

_URL_RE = re.compile(r"https?://\S+")


class EmailSender(ABC):
    """发信接口: 实现必须如实反映投递行为 (发了就是发了, 没发就说没发)。"""

    @abstractmethod
    async def send(self, to: str, subject: str, body: str) -> None:
        """发送纯文本邮件; 投递失败抛异常, 由调用方决定吞或传。"""


class SMTPEmailSender(EmailSender):
    """真实 SMTP 发送 (smtplib 在线程池执行, 不阻塞事件循环)。"""

    def __init__(self, settings: Settings):
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._user = settings.smtp_user
        self._password = settings.smtp_password
        self._from = settings.smtp_from
        self._reply_to = settings.smtp_reply_to

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = subject
        if self._reply_to:
            msg["Reply-To"] = self._reply_to
        msg.set_content(body)
        if self._port == 465:
            # 465: 隐式 TLS (SMTP over SSL), 国内主流邮箱 (QQ/163) 的推荐端口
            with smtplib.SMTP_SSL(self._host, self._port, timeout=15) as smtp:
                smtp.login(self._user, self._password)
                smtp.send_message(msg)
        else:
            # 587/25: 明文连接后 STARTTLS 升级
            with smtplib.SMTP(self._host, self._port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(self._user, self._password)
                smtp.send_message(msg)

    async def send(self, to: str, subject: str, body: str) -> None:
        recipient_hash = hashlib.sha256(normalize_email(to).encode()).hexdigest()[:16]
        try:
            await asyncio.to_thread(self._send_sync, to, subject, body)
        except Exception as exc:  # noqa: BLE001 — caller decides public error policy
            log.error(
                "email_send_failed",
                recipient_hash=recipient_hash,
                transport="smtp",
                error_type=type(exc).__name__,
            )
            raise
        log.info(
            "email_sent",
            recipient_hash=recipient_hash,
            transport="smtp",
            subject=subject,
        )


class DevEmailSender(EmailSender):
    """SMTP 未配置时的开发替身: 只打日志 + (仅 dev) Redis 存演示链接, 不假装发过信。"""

    def __init__(self, settings: Settings, redis):
        self._app_env = settings.app_env
        self._redis = redis

    async def send(self, to: str, subject: str, body: str) -> None:
        link_match = _URL_RE.search(body)
        link = link_match.group(0) if link_match else ""
        # 明确标注：开发模式没有真实发信。日志不包含邮箱、正文、链接或验证码。
        log.warning(
            "dev_email_not_sent",
            recipient_hash=hashlib.sha256(normalize_email(to).encode()).hexdigest()[:16],
            transport="dev",
            email_kind=subject,
            note="开发模式未真实发信: SMTP 未配置, 链接仅落 Redis debug 键",
        )
        if self._app_env == "dev" and link:
            # 仅 dev: 本地演示从 Redis 取链接 (redis-cli GET debug:reset:{email});
            # token 绝不进 HTTP 响应, test/prod 环境连这个键都不写
            await self._redis.setex(
                f"{DEBUG_RESET_KEY_PREFIX}{to}", DEBUG_RESET_TTL_SECONDS, link
            )


def smtp_configured(settings: Settings) -> bool:
    """host/user/password/from 四项齐全才认为 SMTP 可用 (port 恒有默认值)。"""
    return bool(
        settings.smtp_host
        and settings.smtp_user
        and settings.smtp_password
        and settings.smtp_from
    )


def _address(value: str) -> str:
    return parseaddr(value)[1].strip().lower()


def assert_public_registration_email_config(settings: Settings) -> None:
    """Fail fast before serving public registration in production."""

    if settings.app_env != "prod":
        return
    origin = urlparse(settings.web_origin)
    if origin.scheme != "https" or not origin.netloc:
        raise RuntimeError("FATAL: WEB_ORIGIN must be a public HTTPS origin in production")
    if settings.registration_mode == "closed":
        return
    if not smtp_configured(settings):
        raise RuntimeError("FATAL: public registration requires complete SMTP configuration")
    if settings.smtp_host.lower() != "smtp.resend.com":
        raise RuntimeError("FATAL: production public registration requires Resend SMTP")
    if not _address(settings.smtp_from).endswith("@mail.adfgt.xyz"):
        raise RuntimeError("FATAL: SMTP_FROM must use the verified mail.adfgt.xyz domain")
    if not settings.smtp_reply_to or "@" not in _address(settings.smtp_reply_to):
        raise RuntimeError("FATAL: SMTP_REPLY_TO is required for public registration")


def create_email_sender(settings: Settings, redis) -> EmailSender:
    if smtp_configured(settings):
        return SMTPEmailSender(settings)
    if settings.app_env == "prod":
        raise RuntimeError("SMTP_NOT_CONFIGURED")
    return DevEmailSender(settings, redis)
