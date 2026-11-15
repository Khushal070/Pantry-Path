# Model server: Ollama serving the champion model on CPU, OpenAI-compatible on port 8080.
# The weights are baked into the image at build time, so a Cloud Run cold start downloads
# nothing. MODEL comes from the champion record (registry.py passes it):
#   docker build --build-arg MODEL=qwen3:4b-q4_K_M -t pantrypath-model-server ml/serving
ARG OLLAMA_VERSION=0.40.15

# Cloud Run has no GPU: take the Ollama binary and its CPU backends from the official
# image and leave its GPU runtimes behind (CUDA/ROCm/Vulkan, MLX-CUDA with cuDNN and the
# CUDA headers: about 3.6 GB on arm64, 2 GB more on amd64)
FROM ollama/ollama:${OLLAMA_VERSION} AS ollama
RUN rm -rf /usr/lib/ollama/cuda_* /usr/lib/ollama/mlx_cuda_* /usr/lib/ollama/include \
           /usr/lib/ollama/rocm* /usr/lib/ollama/vulkan*

FROM ubuntu:24.04
# ca-certificates: the model pull below; the CPU backends need only glibc and libstdc++
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ollama /usr/bin/ollama /usr/bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama

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
ENTRYPOINT ["/usr/bin/ollama"]
CMD ["serve"]
