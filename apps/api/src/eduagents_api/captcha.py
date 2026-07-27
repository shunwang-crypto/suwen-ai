"""自托管图形验证码 (零外部依赖, Pillow 绘制)。

设计:
- 4 位数字, 干扰线 + 噪点 + 逐字符随机旋转, 输出 PNG data URL;
- 答案存 Redis ``captcha:{id}``, TTL 300s, **校验即删** (GETDEL 原子单次性:
  无论答对答错, 一次校验后即作废, 不给爆破器复用同一张图的机会);
- 数字选取用 ``secrets`` (密码学随机); 视觉噪声用普通 ``random`` (无安全语义)。
"""
from __future__ import annotations

import base64
import io
import random
import secrets
import string
import uuid

from PIL import Image, ImageDraw, ImageFont

CAPTCHA_TTL_SECONDS = 300  # 答案在 Redis 中的存活时间
CAPTCHA_DIGITS = 4
_KEY_PREFIX = "captcha:"

_WIDTH, _HEIGHT = 160, 60
# 深色前景池 (与浅底对比明显, 人眼可辨; 每个字符随机取色)
_INK_COLORS = [(31, 41, 55), (30, 64, 175), (153, 27, 27), (6, 95, 70)]


def _captcha_key(captcha_id: str) -> str:
    return f"{_KEY_PREFIX}{captcha_id}"


def generate_code(length: int = CAPTCHA_DIGITS) -> str:
    """密码学随机的纯数字答案 (secrets, 不用 random)。"""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def render_captcha_png(code: str) -> bytes:
    """Pillow 真实绘图: 浅色底 + 噪点 + 干扰线 + 逐字符旋转, 返回 PNG 字节。"""
    rng = random.Random()  # 仅视觉噪声; 答案随机性在 generate_code (secrets)
    img = Image.new("RGB", (_WIDTH, _HEIGHT), (245, 246, 248))
    draw = ImageDraw.Draw(img)

    # 噪点: 随机散布的灰色像素
    for _ in range(180):
        draw.point(
            (rng.randrange(_WIDTH), rng.randrange(_HEIGHT)),
            fill=(rng.randint(120, 200),) * 3,
        )
    # 干扰线: 3 条跨图随机折线
    for _ in range(3):
        draw.line(
            [
                (rng.randrange(_WIDTH), rng.randrange(_HEIGHT)),
                (rng.randrange(_WIDTH), rng.randrange(_HEIGHT)),
            ],
            fill=(rng.randint(90, 170),) * 3,
            width=rng.randint(1, 2),
        )

    # 逐字符: 独立画布 -> 随机旋转 -> 带透明通道贴回 (Pillow 内嵌字体, 无系统字体依赖)
    font = ImageFont.load_default(size=36)
    x = 12
    for ch in code:
        glyph = Image.new("RGBA", (44, 52), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((6, 2), ch, font=font, fill=rng.choice(_INK_COLORS))
        glyph = glyph.rotate(
            rng.uniform(-28, 28), expand=True, resample=Image.Resampling.BICUBIC
        )
        img.paste(glyph, (x, rng.randint(0, 6)), glyph)
        x += 34

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def create_captcha(redis) -> tuple[str, str]:
    """生成一张验证码: 答案入 Redis (TTL 300s), 返回 (captcha_id, PNG data URL)。"""
    code = generate_code()
    captcha_id = str(uuid.uuid4())
    await redis.setex(_captcha_key(captcha_id), CAPTCHA_TTL_SECONDS, code)
    image = "data:image/png;base64," + base64.b64encode(render_captcha_png(code)).decode()
    return captcha_id, image


async def verify_captcha(redis, captcha_id: str | None, answer: str | None) -> bool:
    """校验并原子删除 (GETDEL): 单次有效, 过期/不存在/答错一律 False。"""
    if not captcha_id or not answer:
        return False
    stored = await redis.getdel(_captcha_key(captcha_id))
    if stored is None:
        return False  # 不存在或已过期 (TTL 到期 Redis 自动删除)
    if isinstance(stored, bytes):  # 兼容 decode_responses=False 的连接
        stored = stored.decode()
    return secrets.compare_digest(stored, answer.strip())
