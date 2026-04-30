FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3-pip \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set python3.11 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

WORKDIR /app

# Install uv for faster dependency resolution
RUN pip install uv

# Copy project files
COPY pyproject.toml .
COPY podracer/ podracer/

# Install dependencies
RUN uv venv && uv sync

# Create directory for audio files
RUN mkdir -p /data

# Default command
ENTRYPOINT ["uv", "run", "transcribe"]
CMD ["--help"]
