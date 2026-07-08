# ── Research Outreach Agent — production image ───────────────────────────────
FROM python:3.12-slim

# System deps for lxml / pdfplumber / dns
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    OPEN_BROWSER=0

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the app
COPY . .

EXPOSE 8000

# Uvicorn serves the FastAPI app + static frontend.
# Uses $PORT so it works on Render/Railway/Fly/etc.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
