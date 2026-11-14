# The yudao assistant host (web_operation_agent_yudao.native_server), built on the public host
# from the wheels deploy.sh brings. The model provider's settings come from an env file the
# owner keeps on the host; nothing secret is built into the image.
FROM python:3.13-slim
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels \
 && useradd --system --uid 10001 woa \
 && mkdir -p /var/lib/woa-yudao && chown woa /var/lib/woa-yudao
USER woa
ENTRYPOINT ["python", "-m", "web_operation_agent_yudao.native_server"]
