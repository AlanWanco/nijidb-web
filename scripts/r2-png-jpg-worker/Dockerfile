FROM nijidb-web:production-0e0e75b

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/migrate_r2_png_to_jpg.py /opt/migrate_r2_png_to_jpg.py
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/app/.venv/bin/python", "-u", "/opt/migrate_r2_png_to_jpg.py"]
