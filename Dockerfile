FROM python:3.12-slim-bookworm

COPY requirements.txt /

ADD li[b] /app/lib
ADD im[g] /app/img

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir -r requirements.txt && \
    rm -rf ~/.cache && rm requirements.txt

WORKDIR /app/lib
ENTRYPOINT ["python3", "main.py"]
LABEL com.centurylinklabs.watchtower.enable="false"
