# Use the full Debian Bullseye image
FROM python:3.10-bullseye

# 1. Force Python to show logs immediately
ENV PYTHONUNBUFFERED=1

# 2. Install Dependencies
# 'libc6-i386' is CRITICAL for KindleGen
RUN apt-get update && apt-get install -y \
    git \
    p7zip-full \
    libpng-dev \
    libjpeg-dev \
    wget \
    libc6-i386 \
    && rm -rf /var/lib/apt/lists/*

# 3. Install KCC
RUN pip install --no-cache-dir git+https://github.com/ciromattia/kcc.git

# 4. Install Mangadex Downloader, Flask AND gallery-dl
RUN pip install --no-cache-dir mangadex-downloader flask requests gallery-dl

# 5. Install KindleGen
RUN wget -O kindlegen.tar.gz "https://archive.org/download/kindlegen2.9/kindlegen_linux_2.6_i386_v2_9.tar.gz" \
    && tar -xzf kindlegen.tar.gz -C /usr/local/bin \
    && rm kindlegen.tar.gz \
    && chmod +x /usr/local/bin/kindlegen

# 6. Setup App
WORKDIR /app
COPY . .
EXPOSE 5000

CMD ["python", "app.py"]
