# Development image: dependencies are immutable; source is mounted by ./local.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PYTHONPATH=/workspace/src:/workspace/examples/yudao/src
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock
WORKDIR /workspace
CMD ["python", "examples/yudao/deploy/local/serve.py"]
