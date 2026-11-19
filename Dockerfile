# 已核ARM64官方基础镜像；固定摘要，避免标签漂移。
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock.txt /app/
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --uid 10001 --create-home app
COPY --chown=10001:10001 . /app/
RUN mkdir -p /app/local && chown -R 10001:10001 /app/local
USER 10001:10001
CMD ["python", "serve.py"]
