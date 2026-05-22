FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/reefy-llm-proxy

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src/reefy_llm_proxy/ ./reefy_llm_proxy/

ENV PYTHONPATH=/opt/reefy-llm-proxy
ENV DATA_DIR=/data
EXPOSE 9080

# Use a non-root user for the runtime, but DATA_DIR is host-mounted
# and the host owner is set by the reconciler (uid 0). For now we
# run as root to avoid permission issues on the credentials.json
# write path. Tighten in a follow-up if needed.

CMD ["python", "-m", "reefy_llm_proxy.main"]
