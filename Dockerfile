FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY quantlab ./quantlab
RUN pip install --no-cache-dir .
ENV QUANTLAB_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "quantlab", "serve", "--host", "0.0.0.0", "--port", "8000"]
