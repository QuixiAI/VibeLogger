FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy proxy script and config
COPY proxy.py .
COPY config.yaml .

# The proxy.py script uses uv run with inline dependencies,
# so we don't need a separate requirements.txt

EXPOSE 8082

CMD ["uv", "run", "proxy.py"]
