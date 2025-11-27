# VibeLogger (QuixiAI)

A lightweight, observable LLM API proxy with built-in tracing for OpenAI, Anthropic, and Google Gemini. Capture and visualize all your LLM traffic using Arize Phoenix. Source: https://github.com/QuixiAI/VibeLogger

## Why this exists

Native OAuth passthrough for the Big 3 agentic CLIs (Claude Code, OpenAI Codex, QuixiAI Gemini CLI fork). You log in with your normal plan; the proxy does not require or manage API keys, and simply forwards traffic while tracing it.

## Features

- 🔄 **Multi-Provider Support**: Proxies OpenAI (`/v1/*`), Anthropic (`/v1/messages/*`), and Gemini (`/v1beta/*`) API endpoints
- 📊 **Built-in Observability**: Automatic OpenTelemetry tracing to Arize Phoenix
- 🌊 **Streaming Support**: Full support for streaming responses with token usage capture
- 🐳 **Docker-Ready**: Complete Docker Compose setup with Phoenix included
- 🔧 **Flexible Configuration**: YAML-based config with environment variable support
- 🎯 **Model-Aware Routing**: Intelligent routing based on model names and API paths

## Use Cases

Perfect for:
- Debugging agentic CLI tools (Claude Code, Gemini CLI, OpenAI Codex) with OAuth passthrough
- Monitoring LLM API usage and costs
- Tracing complex multi-turn conversations
- Analyzing prompt/response patterns
- Capturing token usage across providers

## Compatibility

Scoped to: Claude Code, OpenAI Codex, and the QuixiAI fork of Gemini CLI (with `--proxy`). These work because they use distinct API paths and support OAuth passthrough. Other clients such as open-code, continue.dev, rooCode, cline, or local runtimes (vLLM/sglang/ollama/lmstudio/llamacpp) are untested/unsupported.

## Prerequisites

- Docker and Docker Compose

## Quick Start

1. **Clone and navigate to the project**:
   ```sh
   git clone <your-repo-url>
   cd logging
   ```

2. **Start the services (Daemon Mode)**:
   Run in the background with `-d`:
   ```sh
   docker compose up --build -d
   ```

3. **Access the services**:
   - Phoenix UI: http://localhost:6006
   - Proxy API: http://localhost:8082
   - Health check: http://localhost:8082/health

## Configuration

### Data Directory

By default, Phoenix data is stored in `~/phoenix-data` on your host machine. To store it elsewhere, set the `PHOENIX_DATA_DIR` environment variable before starting docker:

```sh
export PHOENIX_DATA_DIR=/path/to/your/data
mkdir -p $PHOENIX_DATA_DIR
docker compose up -d
```

### Customizing the Proxy Port

If port `8082` is in use, you can change the host port using the `PROXY_PORT` variable:

```sh
# Run the proxy on port 9000 instead
export PROXY_PORT=9000
docker compose up -d
```

### Proxy Configuration

Edit `config.yaml` to customize behavior:

```yaml
# Phoenix collector endpoint (default works with Docker Compose)
phoenix_collector_endpoint: "http://phoenix:6006/v1/traces"

# Optional upstream API base URL overrides
gemini_base_url: null
anthropic_upstream_base: "https://api.anthropic.com"
openai_upstream_base: "https://api.openai.com"
```

## Usage

Authenticate in your client as you normally would (OAuth or existing login/session); the proxy simply forwards traffic and does not store credentials or API keys.
If your client only honors standard proxy variables, set `HTTP_PROXY` / `HTTPS_PROXY` to `http://localhost:8082` (and `NO_PROXY` for any hosts you want to bypass).

### Compatibility map

| Tool | How to point at the proxy | Notes |
| --- | --- | --- |
| Claude Code | `ANTHROPIC_BASE_URL=http://localhost:8082` | Tested |
| OpenAI Codex | `OPENAI_BASE_URL=http://localhost:8082/v1` | Tested |
| Gemini CLI (QuixiAI fork) | `npm install -g github:QuixiAI/gemini-cli` then `gemini --proxy http://localhost:8082 "<prompt>"` | Tested; `--proxy` flag comes from the fork |
| Other OpenAI-compatible SDKs/CLIs | Set `base_url` to `http://localhost:8082/v1` or rely on `HTTP_PROXY` / `HTTPS_PROXY` | Untested; consult client docs |

### Claude / Claude Code

Point Claude tools to the proxy using `ANTHROPIC_BASE_URL`.

```sh
export ANTHROPIC_BASE_URL="http://localhost:8082"
claude
```

### OpenAI / Codex

Point OpenAI-compatible tools to the proxy using `OPENAI_BASE_URL`. Note that for OpenAI, you usually need to append `/v1`.

```sh
export OPENAI_BASE_URL="http://localhost:8082/v1"
codex
```

### Gemini CLI

Standard Gemini CLI tools don't natively support proxies well. Use the [QuixiAI fork of gemini-cli](https://github.com/QuixiAI/gemini-cli) which adds the `--proxy` flag.

```sh
# Install the fork (Node.js)
npm install -g github:QuixiAI/gemini-cli

# Run with proxy flag
gemini --proxy http://localhost:8082 "Hello world"
```

### Standard SDK Usage

**OpenAI Python SDK**
```python
client = OpenAI(
    base_url="http://localhost:8082/v1"
)
```

**Anthropic Python SDK**
```python
client = Anthropic(
    base_url="http://localhost:8082"
)
```

**Google Generative AI SDK**
```python
genai.configure(
    transport="rest",
    client_options={"api_endpoint": "http://localhost:8082"}
)
```

## Architecture

```
┌─────────────┐      ┌──────────────┐      ┌─────────────────┐
│  LLM Client │─────▶│  VibeLogger  │─────▶│ Upstream APIs   │
│             │      │  (Port 8082) │      │ (OpenAI/etc)    │
└─────────────┘      └──────┬───────┘      └─────────────────┘
                            │
                            │ OpenTelemetry
                            │ Traces (OTLP)
                            │
                     ┌──────▼───────┐
                     │    Phoenix   │
                     │ (Port 6006)  │
                     └──────────────┘
```

The proxy:
1. Receives LLM API requests from your clients
2. Creates OpenTelemetry spans with full request/response data
3. Forwards requests to the appropriate upstream API
4. Streams responses back to clients
5. Exports traces to Phoenix for visualization

## Observability

All requests are traced with:
- Request/response bodies (⚠️ may contain sensitive data)
- Token usage statistics
- Latency metrics
- Model and provider information
- Streaming chunk details

View traces in the Phoenix UI at http://localhost:6006 to analyze:
- Request patterns and frequency
- Token consumption per model
- Error rates and types
- Response latencies
- Full conversation flows

## Development

### Running Locally (without Docker)

```sh
# Install dependencies (using uv, or pip install the dependencies listed in proxy.py)
uv pip install aiohttp arize-phoenix-otel opentelemetry-api opentelemetry-sdk pyyaml

# Set environment variables
export COLLECTOR_ENDPOINT="http://localhost:6006/v1/traces"

# Run the proxy
python proxy.py
```

### Project Structure

```
.
├── proxy.py           # Main proxy implementation
├── config.yaml        # Configuration file
├── docker-compose.yml # Docker Compose setup
├── Dockerfile         # Proxy container definition
└── README.md          # This file
```

## Security Considerations

⚠️ **Important**: This proxy logs full request and response bodies for observability. This includes:
- Authentication headers (filtered by default)
- User prompts and conversations
- Model responses
- System messages

**Recommendations**:
- Use only in development/testing environments
- Don't expose the proxy to untrusted networks
- Review Phoenix data retention policies
- Consider adding request body filtering for production use

## Troubleshooting

### Phoenix data directory not found

Create the directory before starting:
```sh
mkdir -p ~/phoenix-data
# or
mkdir -p $PHOENIX_DATA_DIR
```

### Proxy health check failing

Check if the proxy is running:
```sh
curl http://localhost:8082/health
```

### No traces appearing in Phoenix

1. Verify Phoenix is running: http://localhost:6006
2. Check proxy logs: `docker compose logs proxy`
3. Ensure `COLLECTOR_ENDPOINT` is correctly set in docker-compose.yml

## License

MIT (or specify your license)

## Contributing

Contributions welcome! Please open an issue or PR.
