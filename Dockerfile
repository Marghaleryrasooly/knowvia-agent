FROM python:3.12-slim AS runtime

ARG LOCAL_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
ARG EMBEDDING_PROVIDER=local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/knowvia/model-cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/knowvia/model-cache \
    LOCAL_EMBEDDING_MODEL=${LOCAL_EMBEDDING_MODEL} \
    KNOWVIA_EMBEDDING_PROVIDER=${EMBEDDING_PROVIDER} \
    KNOWVIA_ONNX_MODEL_DIR=/opt/knowvia/model-cache/onnx-minilm \
    KNOWVIA_DATA_DIR=/var/lib/knowvia \
    KNOWVIA_DOCUMENTS_DIR=/opt/knowvia/knowledge \
    KNOWVIA_BUNDLED_INDEX=/opt/knowvia/bootstrap/knowvia_index.json \
    PORT=8000

RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/knowvia/app

COPY requirements-production.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements-production.txt

COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && if [ "$EMBEDDING_PROVIDER" = "onnx" ]; then \
         python -m knowvia.onnx_assets --directory "$KNOWVIA_ONNX_MODEL_DIR"; \
       elif [ "$EMBEDDING_PROVIDER" = "local" ]; then \
         pip install --no-cache-dir 'sentence-transformers==5.7.0' \
         && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${LOCAL_EMBEDDING_MODEL}')"; \
       else echo 'Unsupported container embedding provider' >&2; exit 1; fi

COPY knowvia_m1_dataset /opt/knowvia/knowledge
COPY data/knowvia_index.json /opt/knowvia/bootstrap/knowvia_index.json

RUN useradd --create-home --uid 10001 knowvia \
    && mkdir -p /var/lib/knowvia \
    && chown -R knowvia:knowvia /var/lib/knowvia /opt/knowvia/model-cache

USER knowvia

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=3).read()"

CMD ["python", "-m", "knowvia.production"]
