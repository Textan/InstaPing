FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt
COPY InstaPing.py .
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEADLESS=true
CMD ["python", "InstaPing.py"]
