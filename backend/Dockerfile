# AI Shorts Generator - Backend Dockerfile
# Lightweight image with local AI processing (faster-whisper + Ollama)

# Base image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for better caching
COPY backend/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ .

# Create storage directory
RUN mkdir -p /app/storage/jobs

# Create fonts directory and copy Montserrat
RUN mkdir -p /usr/share/fonts/truetype/montserrat
COPY backend/fonts/Montserrat-ExtraBold.ttf /usr/share/fonts/truetype/montserrat/
RUN fc-cache -f

# Set default command
CMD ["python", "main.py"]