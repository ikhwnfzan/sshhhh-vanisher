FROM python:3.12-slim

# ffmpeg is needed by pydub to read and write MP3
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m user
USER user
ENV PATH="/home/user/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ALLOW_LINK=0 \
    MAX_UPLOAD_MB=30 \
    MAX_FILE_MB=15 \
    MAX_MINUTES=6

WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .

# Render gives the port in $PORT. One worker, because the list of trimmed files is kept in memory.
CMD ["sh", "-c", "gunicorn --workers 1 --threads 2 --timeout 600 --bind 0.0.0.0:${PORT:-10000} app:app"]
