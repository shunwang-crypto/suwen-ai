"""URL 网页导入 (对标 ima/元宝的网页剪藏, brief-1 建议 #8)。

``fetch_and_extract(url)`` = 抓取 → 正文抽取 → markdown, 产物直接喂现有两阶段摄取
流水线 (filename 以 ``.md`` 结尾, 复用 MarkdownParser)。

**SSRF 防护 (§8 上传/外联安全)** —— 服务端代抓外链是典型 SSRF 面, 四道闸:

1. 协议白名单: 仅 http/https (``file://`` / ``gopher://`` / ``ftp://`` 一律拒);
2. 主机解析后逐 IP 校验: 回环 (127.0.0.0/8, ::1) / 私网 (10/172.16/192.168, fc00::/7)
   / 链路本地 (169.254.0.0/16 —— 覆盖云元数据地址 169.254.169.254) / 保留段 /
   组播 / 未指定地址 全部拒绝; IPv4-mapped IPv6 (``::ffff:127.0.0.1``) 拆开再校验;
3. 重定向不交给 httpx 自动跟随: 手动最多 3 跳, **每跳重新做第 1、2 步校验**
   (否则 302 到 169.254.169.254 就绕过了首次校验);
4. 响应大小上限 5MB + 20s 超时 + 仅接受 HTML/文本 Content-Type。

已知残余风险: DNS rebinding —— 校验用的解析结果与
httpx 实际连接时的解析可能不同。彻底修需要自定义 transport 把已校验 IP 钉死,
或走出网代理白名单。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

_TIMEOUT_SECONDS = 20.0
_MAX_BYTES = 5 * 1024 * 1024
_MAX_REDIRECTS = 3
_ALLOWED_SCHEMES = ("http", "https")
# UA 伪装: 不少中文站点对默认 python-httpx UA 直接 403
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_ACCEPTED_CONTENT_TYPES = ("text/html", "application/xhtml", "text/plain", "text/markdown")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


class WebFetchError(Exception):
    """网页导入失败。``code`` 直接作为 API 错误信封的 code (中文文案见 middleware)。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _ip_is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16: 云元数据 169.254.169.254 在此
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _resolve_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return [ip]  # 字面量 IP: 无需 DNS
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise WebFetchError("URL_HOST_UNRESOLVABLE", f"域名解析失败: {host}") from exc
    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        addr = info[4][0]
        try:
            resolved.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    if not resolved:
        raise WebFetchError("URL_HOST_UNRESOLVABLE", f"域名解析失败: {host}")
    return resolved


async def validate_url(url: str, *, allow_private: bool = False) -> str:
    """协议 + 主机 IP 段校验; 通过返回规范化 URL, 否则抛 :class:`WebFetchError`。

    ``allow_private`` 仅供本地集成测试 (临时 http server 跑在 127.0.0.1) 打开,
    由 ``WEB_IMPORT_ALLOW_PRIVATE_HOSTS`` 配置驱动, 默认 False。
    """
    candidate = (url or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise WebFetchError("URL_INVALID", "网址格式不合法") from exc
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise WebFetchError("URL_SCHEME_NOT_ALLOWED", "仅支持 http/https 网址")
    host = parsed.hostname
    if not host:
        raise WebFetchError("URL_INVALID", "网址缺少主机名")
    if allow_private:
        return candidate
    for ip in await _resolve_ips(host):
        if _ip_is_internal(ip):
            raise WebFetchError("URL_HOST_NOT_ALLOWED", "该地址指向内网或本机，已拒绝")
    return candidate


async def _get(client: httpx.AsyncClient, url: str, *, allow_private: bool) -> httpx.Response:
    """手动跟随重定向: 每跳都重新过 SSRF 校验 (自动跟随会绕过首次校验)。"""
    current = await validate_url(url, allow_private=allow_private)
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            response = await client.get(current)
        except httpx.HTTPError as exc:
            raise WebFetchError("URL_FETCH_FAILED", f"网页抓取失败: {type(exc).__name__}") from exc
        if response.is_redirect:
            location = response.headers.get("location", "")
            if not location:
                raise WebFetchError("URL_FETCH_FAILED", "重定向缺少 Location 头")
            current = await validate_url(
                str(response.url.join(location)), allow_private=allow_private
            )
            continue
        return response
    raise WebFetchError("URL_FETCH_FAILED", "重定向次数过多")


def _fallback_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    return " ".join(_TAG_RE.sub("", match.group(1)).split())[:200]


def _dedupe_blocks(markdown: str) -> str:
    """去重重复段落 (保留首次出现)。

    trafilatura 在部分页面结构 (article + nav/footer 混排) 下会把同一段正文
    输出两遍; 站点的固定页眉页脚也常被重复抓到。只对 ≥40 字的长块去重, 避免
    误删列表项、公式行这类合法重复的短行。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for block in markdown.split("\n\n"):
        key = "".join(block.split())
        if len(key) >= 40:
            if key in seen:
                continue
            seen.add(key)
        kept.append(block)
    return "\n\n".join(kept)


def _extract_sync(html: str, url: str) -> tuple[str, str]:
    """trafilatura 正文抽取 (CPU 密集, 由调用方丢线程池)。"""
    import trafilatura

    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_tables=True,
        include_comments=False,
        include_links=False,
        url=url,
    )
    title = ""
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
    except Exception:  # noqa: BLE001 — 元数据抽取失败不该让整次导入失败
        meta = None
    if meta is not None and getattr(meta, "title", None):
        title = str(meta.title).strip()
    if not title:
        title = _fallback_title(html)
    return title, _dedupe_blocks((text or "").strip())


async def fetch_and_extract(url: str, *, allow_private: bool = False) -> tuple[str, str]:
    """抓取网页并抽取正文, 返回 ``(标题, markdown 正文)``。

    失败一律抛 :class:`WebFetchError` (带可映射为中文的 code), 不向外漏裸异常。
    """
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    ) as client:
        response = await _get(client, url, allow_private=allow_private)

    if response.status_code >= 400:
        raise WebFetchError("URL_FETCH_FAILED", f"目标站点返回 HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not any(t in content_type for t in _ACCEPTED_CONTENT_TYPES):
        raise WebFetchError("URL_CONTENT_UNSUPPORTED", "该网址不是网页正文 (仅支持 HTML/文本)")
    if len(response.content) > _MAX_BYTES:
        raise WebFetchError("URL_CONTENT_TOO_LARGE", "网页内容过大，已拒绝")

    html = response.text
    title, body = await asyncio.to_thread(_extract_sync, html, str(response.url))
    if not body:
        raise WebFetchError("URL_EXTRACT_EMPTY", "未能从该网页提取到正文内容")
    title = title or "未命名网页"
    # 正文顶部补一级标题: 下游 MarkdownParser 据此建 heading_path (引用可定位到来源)
    markdown = body if body.lstrip().startswith("#") else f"# {title}\n\n{body}"
    markdown = f"{markdown}\n\n---\n\n> 来源: {response.url}\n"
    return title, markdown
