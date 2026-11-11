# Evaluation runner: the model pipeline's harnesses with pinned dependencies.
# Build from the repo root (it needs shared/, data-pipeline/ and data/golden/):
#   docker build -f ml/Dockerfile -t pantrypath-ml .
# Pull the DVC catalog first (it is copied in), then run against a model server on the host:
#   docker run --rm -e MODEL_SERVER_URL=http://host.docker.internal:11434 \
#     -v "$PWD/ml/reports:/app/ml/reports" pantrypath-ml \
#     pantrypath_ml.evaluate_a2 --candidate qwen3-4b --split dev --no-mlflow
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MLFLOW_DISABLE_AGENT_HINT=1
WORKDIR /app

COPY ml/requirements.txt ml/requirements.txt
RUN pip install --no-cache-dir -r ml/requirements.txt

COPY shared shared
COPY data-pipeline data-pipeline
COPY data/golden data/golden
COPY ml ml
RUN pip install --no-cache-dir --no-deps -e shared -e data-pipeline -e ml

WORKDIR /app/ml
ENTRYPOINT ["python", "-m"]
CMD ["pantrypath_ml.evaluate_a2", "--help"]
