# Base: Python 3.12 (Lightweight)
FROM python:3.12-slim

# Prevent python buffering
ENV PYTHONUNBUFFERED=1

# Install System Dependencies
# 7zip is required by KCC. git is required for pip to install from the repo.
RUN apt-get update && apt-get install -y \
    git \
    curl \
    p7zip-full \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install KindleGen (MANDATORY)
# KCC cannot create MOBI/Kindle files without this binary.
# We download it to /usr/bin so it's globally available.
RUN curl -L -o /usr/bin/kindlegen https://archive.org/download/kindlegen_linux_2_9/kindlegen \
    && chmod +x /usr/bin/kindlegen

# -----------------------------------------------------------------
# THE "STRAIGHT FROM REPO" MAGIC
# This installs the latest KCC directly from GitHub as a package.
# -----------------------------------------------------------------
RUN pip install --no-cache-dir git+https://github.com/ciromattia/kcc.git

# Install our Web UI dependencies (Flask)
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy App Code
COPY app.py .
COPY templates templates/

# Create folders
RUN mkdir -p /app/uploads /app/processed

# Expose Port
EXPOSE 8080

# Run
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "--timeout", "3600", "app:app"]
