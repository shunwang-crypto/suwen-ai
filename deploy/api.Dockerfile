FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

COPY packages/ packages/
COPY apps/api/ apps/api/
COPY apps/worker/ apps/worker/
COPY alembic.ini ./
COPY alembic/ alembic/

RUN pip install --no-cache-dir -e packages/core -e apps/api -e apps/worker

EXPOSE 8000
CMD ["uvicorn", "eduagents_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=172.16.0.0/12"]
