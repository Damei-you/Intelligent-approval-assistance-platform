FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

# 腾讯云服务器优先使用同地域软件源；需要在其他环境构建时可通过 --build-arg 覆盖。
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY app ./app
COPY main.py ./main.py

# 容器使用普通用户运行 API 和 Celery，降低应用进程拥有 root 权限的风险。
# 上传目录预先设置为该用户可写，使首次挂载 Docker 数据卷时能够保留正确权限。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/storage/uploads \
    && chown -R appuser:appuser /app/storage

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
