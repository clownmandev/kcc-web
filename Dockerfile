# Use a lightweight Python base image
FROM python:3.10-slim

# Install system dependencies required by KCC (7zip is crucial for comic archives)
RUN apt-get update && apt-get install -y \
    git \
    p7zip-full \
    libpng-dev \
    libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

# Install KCC directly from the official repository to get the latest version
RUN pip install --no-cache-dir git+https://github.com/ciromattia/kcc.git

# Install Flask for the web interface
RUN pip install --no-cache-dir flask

# Set working directory
WORKDIR /app

# Copy your application code
COPY . .

# Expose the port the app runs on
EXPOSE 5000

# Run the application
CMD ["python", "app.py"]
