FROM python:3.11-slim

# System deps: ffmpeg (video decoding), libgl + libglib (OpenCV headless)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Install CPU-only PyTorch first so ultralytics doesn't pull the full NVIDIA GPU stack
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Bake YOLOv8 weights into the image (downloads to /app/yolov8s.pt) so the
# pipeline runs fully offline with no cold-start download per container.
RUN python -c "from ultralytics import YOLO; YOLO('yolov8s.pt')"

COPY . .

# API default; pipeline overrides CMD via docker compose run
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
