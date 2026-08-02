FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# D1 锁定纪律: 镜像依赖以 requirements.lock 为准 (pyproject 声明曾漂移,
# 容器缺 langgraph 由 M1-D1 compose 验证抓出)。conda 本地路径行需剔除。
COPY requirements.lock ./
RUN grep -v 'file:///' requirements.lock > /tmp/req.lock \
    && pip install --no-cache-dir -r /tmp/req.lock

COPY packages/ packages/
COPY apps/api/ apps/api/
COPY apps/worker/ apps/worker/
COPY scripts/create_first_admin.py scripts/create_first_admin.py
COPY scripts/setup_langgraph_checkpoint.py scripts/setup_langgraph_checkpoint.py
COPY alembic.ini ./
COPY alembic/ alembic/

RUN pip install --no-cache-dir --no-deps -e packages/core -e apps/api -e apps/worker

EXPOSE 8000
CMD ["uvicorn", "eduagents_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=172.16.0.0/12"]
