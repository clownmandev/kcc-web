# Base image: Modern Python 3.12 on lightweight Debian
FROM python:3.12-slim

# Prevent python from buffering stdout (better logs)
ENV PYTHONUNBUFFERED=1

# Install system dependencies (7zip is required by KCC)
RUN apt-get update && apt-get install -y \
    git \
    p7zip-full \
    curl \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install KindleGen (The hardest part - pulling from Internet Archive or stable mirror)
# We place it in /usr/bin so KCC can find it automatically.
RUN curl -L -o /usr/bin/kindlegen https://archive.org/download/kindlegen_linux_2_9/kindlegen \
    && chmod +x /usr/bin/kindlegen

# Clone the LATEST KCC from the official repo
WORKDIR /app
RUN git clone https://github.com/ciromattia/kcc.git kcc-source

# Install KCC dependencies
WORKDIR /app/kcc-source
RUN pip install --no-cache-dir -r requirements.txt

# Install our Web UI dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy our Web App code
COPY app.py .
COPY templates templates/

# Create directories for uploads/processing
RUN mkdir -p /app/uploads /app/processed

# Expose the web port
EXPOSE 8080

# Run the Web App
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "3600", "app:app"]
