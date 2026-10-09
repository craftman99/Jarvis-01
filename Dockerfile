FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[all]"
# Jarvis keeps his memory (database, YouTube token) in /app/data and videos in /app/videos.
VOLUME ["/app/data", "/app/videos"]
ENV JARVIS_HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["jarvis", "web"]
