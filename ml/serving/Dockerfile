# Model server: Ollama serving the champion model on CPU, OpenAI-compatible on port 8080.
# The weights are baked into the image at build time, so a Cloud Run cold start downloads
# nothing. MODEL comes from the champion record (registry.py passes it):
#   docker build --build-arg MODEL=qwen3:4b-q4_K_M -t pantrypath-model-server ml/serving
ARG OLLAMA_VERSION=0.40.15
FROM ollama/ollama:${OLLAMA_VERSION}

ARG MODEL
# Keep the model loaded between requests, one request at a time (Cloud Run concurrency 1),
# and leave room for the refine prompt plus its reply
ENV OLLAMA_HOST=0.0.0.0:8080 \
    OLLAMA_MODELS=/models \
    OLLAMA_KEEP_ALIVE=-1 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_CONTEXT_LENGTH=8192

RUN test -n "$MODEL" || (echo "build with --build-arg MODEL=<ollama model id>" && exit 1); \
    ollama serve & pid=$!; \
    until ollama list >/dev/null 2>&1; do sleep 1; done; \
    ollama pull "$MODEL" && kill "$pid"

EXPOSE 8080
ENTRYPOINT ["/bin/ollama"]
CMD ["serve"]
