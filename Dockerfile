FROM python:3.11-slim-bookworm AS core
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MODEL_CACHE_DIR=/app/models \
    HF_HUB_CACHE=/app/models \
    STATE_DIR=/app/state
COPY requirements-core.txt .
RUN pip install --no-cache-dir -r requirements-core.txt
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/state /app/models \
    && chown -R appuser:appuser /app

FROM core AS test
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt
COPY app ./app
COPY data ./data
COPY tests ./tests
COPY tools ./tools
COPY scripts ./scripts
COPY docs/validation ./docs/validation
USER appuser
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM core AS runtime
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${TORCH_INDEX_URL}" torch==2.6.0 torchvision==0.21.0 \
    && pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data ./data
COPY scripts/check_inference_runtime.py ./scripts/check_inference_runtime.py
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"
CMD ["python", "-m", "app.main", "--mode", "scheduler"]
