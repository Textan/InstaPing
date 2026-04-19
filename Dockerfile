FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg apt-transport-https ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium
RUN playwright install-deps chromium

COPY InstaPing.py .

ENV HEADLESS=true
CMD ["python", "InstaPing.py"]
