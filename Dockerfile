# Use a lightweight Python base image
FROM python:3.10-slim

# Install system dependencies
# libc6-i386 is REQUIRED for kindlegen (32-bit)
RUN apt-get update && apt-get install -y \
    git \
    p7zip-full \
    libpng-dev \
    libjpeg-dev \
    wget \
    libc6-i386 \
    && rm -rf /var/lib/apt/lists/*

# Install KCC
RUN pip install --no-cache-dir git+https://github.com/ciromattia/kcc.git

# Install Flask and Requests (for API calls)
RUN pip install --no-cache-dir flask requests

# Install MangaDex Downloader
RUN pip install --no-cache-dir mangadex-downloader

# --- Install KindleGen ---
RUN wget -O kindlegen.tar.gz "https://archive.org/download/kindlegen2.9/kindlegen_linux_2.6_i386_v2_9.tar.gz" \
    && tar -xzf kindlegen.tar.gz -C /usr/local/bin \
    && rm kindlegen.tar.gz

# Set working directory
WORKDIR /app

# Copy your application code
COPY . .

# Expose the port
EXPOSE 5000

# Run the application
CMD ["python", "app.py"]
