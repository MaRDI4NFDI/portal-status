FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ ./static/

ENV PORT=8080
EXPOSE 8080

# Threaded workers: requests are I/O-bound on Prometheus, and the cache
# means most of them never leave the process at all.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "30", "--access-logfile", "-", "app:app"]
