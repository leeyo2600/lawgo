FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY web ./web
RUN useradd --uid 10001 --create-home lawgo && mkdir /data && chown lawgo:lawgo /data
USER lawgo
ENV LAWGO_HOST=0.0.0.0 PORT=8000 LAWGO_DATA_DIR=/data
ENV LAWGO_EMBEDDING_CACHE=/data/embedding_cache
EXPOSE 8000
CMD ["python", "web_app.py"]
