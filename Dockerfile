# Use the full Debian Bullseye image (not slim) to ensure all libraries exist
FROM python:3.10-bullseye

# 1. Force Python to show logs immediately (Fixes "No logs" issue)
ENV PYTHONUNBUFFERED=1

# 2. Install Dependencies
# 'libc6-i386' is CRITICAL for KindleGen (which is a 32-bit app)
RUN apt-get update && apt-get install -y \
    git \
    p7zip-full \
    libpng-dev \
    libjpeg-dev \
    wget \
    libc6-i386 \
    && rm -rf /var/lib/apt/lists/*

# 3. Install KCC (Kindle Comic Converter)
RUN pip install --no-cache-dir git+https://github.com/ciromattia/kcc.git

# 4. Install MangaDex Downloader & Flask
RUN pip install --no-cache-dir mangadex-downloader flask requests

# 5. Install KindleGen (The "MOBI Fix")
# We download it, extract it to /usr/local/bin, and make it executable
RUN wget -O kindlegen.tar.gz "https://archive.org/download/kindlegen2.9/kindlegen_linux_2.6_i386_v2_9.tar.gz" \
    && tar -xzf kindlegen.tar.gz -C /usr/local/bin \
    && rm kindlegen.tar.gz \
    && chmod +x /usr/local/bin/kindlegen

# 6. Setup App
WORKDIR /app
COPY . .
EXPOSE 5000

# 7. Run Command
CMD ["python", "app.py"]
