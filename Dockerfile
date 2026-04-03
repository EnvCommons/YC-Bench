FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt update && apt upgrade -y && apt install -y \
    software-properties-common \
    ca-certificates \
    python3 \
    python3-pip \
    curl \
    git \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt update \
    && apt install -y python3.12 python3.12-venv python3.12-dev \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
RUN uv venv --python 3.12

COPY . /app
RUN uv pip install -r /app/requirements.txt

ENV YC_BENCH_BIN="/app/.venv/bin/yc-bench"

EXPOSE 8080
CMD ["uv", "run", "python", "/app/server.py"]
