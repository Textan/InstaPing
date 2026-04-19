FROM python:3.11-slim

   RUN apt-get update && apt-get install -y \
       wget \
       gnupg \
       apt-transport-https \
       ca-certificates \
       && rm -rf /var/lib/apt/lists/*

   RUN apt-get update && apt-get install -y \
       chromium-browser \
       && rm -rf /var/lib/apt/lists/*

   WORKDIR /app

   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt

   COPY InstaPing.py .

   ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
   ENV HEADLESS=true

   CMD ["python", "InstaPing_Improved.py"]
