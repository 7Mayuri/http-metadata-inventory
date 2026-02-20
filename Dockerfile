# ───────────────────────────────────────────────────
# Stage 1 – Python 3.12 slim base
# ───────────────────────────────────────────────────
FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ───────────────────────────────────────────────────
# Stage 2 – Install dependencies (cached layer)
# ───────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ───────────────────────────────────────────────────
# Stage 3 – Copy application code
# ───────────────────────────────────────────────────
COPY . .

# ───────────────────────────────────────────────────
# Expose the port and set the default command
# ───────────────────────────────────────────────────
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
