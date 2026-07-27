"""图形验证码单测 (fakeredis, 零服务依赖): 生成 / 单次性 / 过期 / 边界。"""
import base64
import uuid

import pytest
from eduagents_api.captcha import (
    CAPTCHA_TTL_SECONDS,
    create_captcha,
    generate_code,
    render_captcha_png,
    verify_captcha,
)
from fakeredis import FakeAsyncRedis

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


def test_generate_code_four_digits():
    for _ in range(20):
        code = generate_code()
        assert len(code) == 4 and code.isdigit()


def test_render_png_real_image():
    """真实 Pillow 绘图: 输出必须是合法 PNG (魔数), 且两次渲染不逐字节相同 (有随机噪声)。"""
    png1 = render_captcha_png("1234")
    png2 = render_captcha_png("1234")
    assert png1.startswith(PNG_MAGIC) and png2.startswith(PNG_MAGIC)
    assert len(png1) > 500  # 有实际内容, 不是空白占位图
    assert png1 != png2  # 干扰线/噪点/旋转随机 -> 同一答案图像不同


async def test_create_captcha_stores_answer_with_ttl(redis):
    captcha_id, image = await create_captcha(redis)
    uuid.UUID(captcha_id)  # id 是合法 uuid
    assert image.startswith("data:image/png;base64,")
    raw = base64.b64decode(image.split(",", 1)[1])
    assert raw.startswith(PNG_MAGIC)
    stored = await redis.get(f"captcha:{captcha_id}")
    assert stored is not None and len(stored) == 4 and stored.isdigit()
    ttl = await redis.ttl(f"captcha:{captcha_id}")
    assert 0 < ttl <= CAPTCHA_TTL_SECONDS


async def test_verify_correct_then_single_use(redis):
    captcha_id, _ = await create_captcha(redis)
    answer = await redis.get(f"captcha:{captcha_id}")
    assert await verify_captcha(redis, captcha_id, answer) is True
    # 单次性: 校验即删, 同一答案第二次必失败
    assert await verify_captcha(redis, captcha_id, answer) is False
    assert await redis.get(f"captcha:{captcha_id}") is None


async def test_verify_wrong_answer_also_consumes(redis):
    """答错同样消耗 (校验即删): 不给爆破器拿同一张图反复试的机会。"""
    captcha_id, _ = await create_captcha(redis)
    answer = await redis.get(f"captcha:{captcha_id}")
    wrong = "0000" if answer != "0000" else "1111"
    assert await verify_captcha(redis, captcha_id, wrong) is False
    assert await redis.get(f"captcha:{captcha_id}") is None
    # 答案对了也晚了: key 已删
    assert await verify_captcha(redis, captcha_id, answer) is False


async def test_verify_expired_or_missing(redis):
    """过期 (TTL 到期 Redis 删 key) 与不存在等价: 一律 False。"""
    assert await verify_captcha(redis, str(uuid.uuid4()), "1234") is False


async def test_verify_blank_inputs(redis):
    captcha_id, _ = await create_captcha(redis)
    assert await verify_captcha(redis, None, "1234") is False
    assert await verify_captcha(redis, captcha_id, None) is False
    assert await verify_captcha(redis, "", "") is False
    # 空入参不消耗已生成的验证码
    assert await redis.get(f"captcha:{captcha_id}") is not None


async def test_verify_strips_whitespace(redis):
    captcha_id, _ = await create_captcha(redis)
    answer = await redis.get(f"captcha:{captcha_id}")
    assert await verify_captcha(redis, captcha_id, f"  {answer} ") is True
