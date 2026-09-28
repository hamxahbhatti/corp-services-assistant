# Container image for the web app + MCP tool server (for Azure Container Apps or any container host).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-azure.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-azure.txt
COPY . .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "python -m app.tools.enterprise_mcp & python -m app.knowledge.ingest && uvicorn app.server:app --host 0.0.0.0 --port 8000"]
