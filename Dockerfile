FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV AURUM_NO_BROWSER=1
CMD ["python", "app.py"]
