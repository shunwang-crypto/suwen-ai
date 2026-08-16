"""上传体的分块限流读 (P1-1, 唯一实现)。

此前同一"每 1MB 累计、超限立即断开"的循环存在四份拷贝 (kb / chat_attachments /
tutor_attachments / resources), 错误码各自演化 —— 收敛到本模块, 新上传端点一律
import read_upload_capped, 不再落地本地副本。

语义契约 (与四份旧实现保持行为不变):
- 分块 1MB 累计, 不把任意大的 multipart 先全量吞进内存再判大小;
- max_bytes <= 0 视为不限量 (tutor/resources 的 settings 可配 0 关闭上限);
- 超限抛 413, detail 由调用方传入 (保留各自既有的错误码, 前端文案依赖它们);
- reject_empty=True 时空文件抛 400 EMPTY_FILE (仅 chat 附件沿用此行为)。
"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

_CHUNK = 1024 * 1024


async def read_upload_capped(
    file: UploadFile,
    max_bytes: int,
    *,
    too_large_detail: str = "FILE_TOO_LARGE",
    reject_empty: bool = False,
) -> bytes:
    buf = bytearray()
    while True:
        part = await file.read(_CHUNK)
        if not part:
            break
        buf.extend(part)
        if max_bytes > 0 and len(buf) > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)
    if reject_empty and not buf:
        raise HTTPException(status_code=400, detail="EMPTY_FILE")
    return bytes(buf)
