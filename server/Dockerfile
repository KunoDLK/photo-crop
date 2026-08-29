# Tile server image. Uses OpenCV's OpenCL path for resampling when a GPU (or any
# OpenCL device) is present, otherwise falls back to CPU.
#
# Build from the repo root:  docker build -f server/Dockerfile .
#
# - ocl-icd-libopencl1 provides the OpenCL ICD loader (libOpenCL.so.1).
# - The nvidia.icd file registers the NVIDIA platform; the actual
#   libnvidia-opencl.so.1 is injected at runtime by nvidia-container-toolkit when
#   the container is started with `--gpus all`. Without a GPU the loader finds no
#   platform and OpenCV transparently falls back to CPU (INTER_AREA).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ocl-icd-libopencl1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/OpenCL/vendors \
    && printf 'libnvidia-opencl.so.1\n' > /etc/OpenCL/vendors/nvidia.icd

WORKDIR /app

COPY server/requirements.txt .
RUN pip install -r requirements.txt

COPY server/ /app/server/

EXPOSE 8000

CMD ["python", "-m", "server.main"]
