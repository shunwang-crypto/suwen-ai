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
# Runtime prompt templates are loaded from /app/prompts by the API graphs,
# moderation layer and worker jobs. Keep the whole directory in the shared
# API/worker image so newly added templates are not omitted one by one.
COPY prompts/ prompts/
COPY scripts/create_first_admin.py scripts/create_first_admin.py
COPY scripts/setup_langgraph_checkpoint.py scripts/setup_langgraph_checkpoint.py
COPY alembic.ini ./
COPY alembic/ alembic/

# Fail during image construction instead of at the first production request.
RUN test -f /app/prompts/qa_web_query_plan.md \
    && test -f /app/prompts/plan_draft.md \
    && test -f /app/prompts/chat_reflect.md \
    && test -f /app/prompts/minor_safety_guard.md

RUN pip install --no-cache-dir --no-deps -e packages/core -e apps/api -e apps/worker

EXPOSE 8000
CMD ["uvicorn", "eduagents_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=172.16.0.0/12"]
