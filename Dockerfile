# Standalone build (`docker build .`). docker-compose builds the same app from
# backend/Dockerfile; keep the two in sync if you change one.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg is shelled out to by every render stage; libgl1/libglib2.0-0 are
# required by OpenCV and MediaPipe; fontconfig resolves caption fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend/ /app/

RUN mkdir -p /usr/share/fonts/truetype/montserrat
COPY backend/fonts/ /usr/share/fonts/truetype/montserrat/
RUN fc-cache -f

# STORAGE_ROOT defaults to /app/storage inside a container (see config.py).
RUN mkdir -p /app/storage/jobs /app/storage/logs

EXPOSE 5001
CMD ["python", "main.py"]
