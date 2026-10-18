# Quixi VibeLogger

A lightweight, observable LLM API proxy with built-in tracing for OpenAI, Anthropic, Google Gemini, and Meta Muse. Capture and visualize all your LLM traffic using Arize Phoenix.

## Why this exists

Native OAuth passthrough for the Big 3 agentic CLIs (Claude Code, OpenAI Codex, Gemini CLI). You log in with your normal plan; the proxy does not require or manage API keys, and simply forwards traffic while tracing it.

## Features

- 🔄 **Multi-Provider Support**: Proxies OpenAI (`/v1/*`), Anthropic (`/v1/messages/*`), Gemini (`/v1beta/*`), and Meta Muse (`/meta/v1/*`, `/muse-code/*`) API endpoints
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

Scoped to: Claude Code, OpenAI Codex, the QuixiAI fork of Gemini CLI (with `--base-url`), and Meta Muse (with `--base-url`). Muse routing was verified against local mock upstreams with Muse 1.4.6-R6000.1; a live authenticated Meta completion has not been verified. Other clients such as open-code, continue.dev, rooCode, cline, or local runtimes (vLLM/sglang/ollama/lmstudio/llamacpp) are untested/unsupported.

## Prerequisites

- Docker and Docker Compose

## Quick Start

1. **Clone and navigate to the project**:
   ```sh
   git clone https://github.com/QuixiAI/VibeLogger.git
   cd VibeLogger
   ```

2. **Start the services (Daemon Mode)**:
   Run in the background with `-d`:
   ```sh
   mkdir -p ~/phoenix-data
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
meta_upstream_base: "https://api.meta.ai"
```

## Usage

Authenticate in your client as you normally would (OAuth or existing login/session); the proxy simply forwards traffic and does not store credentials or API keys.
If your client only honors standard proxy variables, set `HTTP_PROXY` / `HTTPS_PROXY` to `http://localhost:8082` (and `NO_PROXY` for any hosts you want to bypass).

### Compatibility map

| Tool | How to point at the proxy |
| --- | --- |
| Claude Code | `ANTHROPIC_BASE_URL=http://localhost:8082` |
| Meta Muse | `muse --base-url http://localhost:8082/meta/v1` |
| OpenAI Codex | `OPENAI_BASE_URL=http://localhost:8082/v1` |
| Gemini CLI (QuixiAI fork) | `npm install -g github:QuixiAI/gemini-cli` then `gemini -url http://localhost:8082 "<prompt>"` |

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

### Meta Muse

Keep your normal Muse login and override the provider URL:

```sh
muse --base-url http://localhost:8082/meta/v1
# Headless runs:
muse exec --base-url http://localhost:8082/meta/v1 "Reply with OK."
```

The proxy strips `/meta` and forwards `/v1/responses` (including streaming),
other `/v1/*` provider calls, and Muse's `/meta/muse-code/*` and origin-relative `/muse-code/*` calls to
`https://api.meta.ai`. It preserves Muse's authorization and session headers,
and records request/response bodies and token usage in Phoenix. The separate
prefix allows Claude Code and OpenAI clients to keep their existing settings.
Muse's launcher downloads and login traffic are separate from provider requests
and are not redirected by this option.

After updating the proxy source, rebuild just the proxy service:

```sh
cd ~/VibeLogger
docker compose up -d --build --no-deps proxy
```

This recreates the proxy container and briefly interrupts active streams; run it
between Claude Code requests. Phoenix's container and stored traces remain in place.

### Gemini CLI

Standard Gemini CLI tools don't natively support proxies well. Use the [QuixiAI fork of gemini-cli](https://github.com/QuixiAI/gemini-cli) which adds the `--base-url` flag.

```sh
# Install gemini-cli
npm install -g github:QuixiAI/gemini-cli

# Run with --base-url flag
gemini --base-url http://localhost:8082 "Hello world"
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

## License

MIT

## Contributing

Contributions welcome! Please open an issue or PR.
