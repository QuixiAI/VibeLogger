#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "aiohttp",
#     "arize-phoenix-otel",
#     "opentelemetry-api",
#     "opentelemetry-sdk",
#     "pyyaml",
# ]
# ///

import json
import logging
import yaml
from typing import Dict, Any, List
from aiohttp import web
import aiohttp

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from openinference.semconv.trace import SpanAttributes

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Load configuration
def load_config():
    try:
        with open("config.yaml", "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("config.yaml not found, using defaults")
        return {}


import os

config = load_config()
PHOENIX_COLLECTOR_ENDPOINT = config.get("phoenix_collector_endpoint")
GEMINI_BASE_URL = config.get("gemini_base_url")
ANTHROPIC_UPSTREAM_BASE = config.get("anthropic_upstream_base")
OPENAI_UPSTREAM_BASE = config.get("openai_upstream_base")
DEFAULT_ANTHROPIC_API_BASE = "https://api.anthropic.com"
DEFAULT_OPENAI_API_BASE = "https://api.openai.com"

# API Keys from environment
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}


def sanitize_headers(request: web.Request) -> Dict[str, str]:
    """Remove hop-by-hop headers so aiohttp can manage them."""
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def is_openai_model(model_name: str) -> bool:
    """Check if a model name is an OpenAI/ChatGPT model."""
    if not model_name:
        return False
    openai_prefixes = ("gpt-", "text-", "davinci", "curie", "babbage", "ada", "chatgpt-", "o1-")
    return model_name.lower().startswith(openai_prefixes)


def is_anthropic_model(model_name: str) -> bool:
    """Check if a model name is an Anthropic/Claude model."""
    if not model_name:
        return False
    return "claude" in model_name.lower()

# Setup Manual Phoenix Tracing
tracer = None
if PHOENIX_COLLECTOR_ENDPOINT:
    resource = Resource(attributes={"service.name": "gemini-proxy"})
    tracer_provider = TracerProvider(resource=resource)

    # Phoenix expects traces at /v1/traces usually
    # If the user provided endpoint doesn't have it, we might need to adjust,
    # but OTLPSpanExporter usually takes the full URL.
    # The user's config default is "http://localhost:6006/v1/traces"
    span_exporter = OTLPSpanExporter(endpoint=PHOENIX_COLLECTOR_ENDPOINT)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)
    tracer = trace.get_tracer(__name__)
    logger.info(f"Phoenix tracing enabled to {PHOENIX_COLLECTOR_ENDPOINT}")
else:
    logger.info("Phoenix tracing disabled")
    tracer = trace.get_tracer(__name__)


def reassemble_streaming_response(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reassemble streaming chunks into a single response as if non-streaming"""
    try:
        if not chunks:
            return {}

        # Start with the first chunk as base
        # We assume the chunks are the JSON representation of the API response
        reassembled = json.loads(json.dumps(chunks[0]))  # Deep copy

        # If there's only one chunk, return it
        if len(chunks) == 1:
            return reassembled

        # Merge subsequent chunks
        for chunk in chunks[1:]:
            # Merge candidates
            if "candidates" in chunk and chunk["candidates"]:
                if "candidates" not in reassembled:
                    reassembled["candidates"] = []

                for new_candidate in chunk["candidates"]:
                    # Find matching candidate or append new one
                    # Usually streaming responses have one candidate at index 0
                    # But index might be present
                    index = new_candidate.get("index", 0)

                    # Ensure reassembled has enough slots
                    while len(reassembled["candidates"]) <= index:
                        reassembled["candidates"].append(
                            {
                                "index": len(reassembled["candidates"]),
                                "content": {"parts": [], "role": "model"},
                            }
                        )

                    existing = reassembled["candidates"][index]

                    # Merge content parts
                    if (
                        "content" in new_candidate
                        and "parts" in new_candidate["content"]
                    ):
                        if "content" not in existing:
                            existing["content"] = {"role": "model", "parts": []}
                        if "parts" not in existing["content"]:
                            existing["content"]["parts"] = []

                        existing["content"]["parts"].extend(
                            new_candidate["content"]["parts"]
                        )

                    # Update finish reason if present
                    if "finishReason" in new_candidate:
                        existing["finishReason"] = new_candidate["finishReason"]

                    # Update safety ratings if present (usually in the last chunk or when they change)
                    if "safetyRatings" in new_candidate:
                        existing["safetyRatings"] = new_candidate["safetyRatings"]

            # Update usage metadata (use latest)
            if "usageMetadata" in chunk:
                reassembled["usageMetadata"] = chunk["usageMetadata"]

            # Update other fields
            for key in ["modelVersion"]:  # responseId might be in all
                if key in chunk:
                    reassembled[key] = chunk[key]

        return reassembled
    except Exception as e:
        logger.error(f"Error reassembling streaming response: {e}", exc_info=True)
        return {}


async def handle_generate_content(request):
    """Pure pass-through HTTP proxy - forwards requests directly to Gemini API"""
    model = request.match_info.get("model")

    # Read the request body
    try:
        body = await request.read()
        body_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return web.Response(status=400, text="Invalid JSON body")

    # Determine if streaming based on endpoint
    is_streaming = "streamGenerateContent" in request.path

    # Build upstream URL preserving query parameters
    upstream_base = GEMINI_BASE_URL or "https://generativelanguage.googleapis.com"
    upstream_url = f"{upstream_base}{request.rel_url}"

    # Start tracing span
    with tracer.start_as_current_span(
        "gemini_request", kind=trace.SpanKind.SERVER
    ) as span:
        span.set_attribute("llm.model", model)
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.url", request.path)
        span.set_attribute(SpanAttributes.INPUT_VALUE, body.decode("utf-8", errors="ignore") if body else "")

        try:
            # Forward request to upstream API
            async with aiohttp.ClientSession() as session:
                headers = sanitize_headers(request)

                async with session.post(
                    upstream_url,
                    data=body,
                    headers=headers,
                ) as upstream_response:

                    if is_streaming:
                        # Filter headers to avoid conflicts
                        response_headers = {}
                        for key, value in upstream_response.headers.items():
                            # Skip headers that should be set by aiohttp
                            if key.lower() not in ['content-length', 'transfer-encoding', 'content-encoding']:
                                response_headers[key] = value

                        # Stream response back
                        resp = web.StreamResponse(
                            status=upstream_response.status,
                            headers=response_headers,
                        )
                        await resp.prepare(request)

                        # Stream chunks and collect for logging
                        chunks_buffer = []
                        try:
                            async for chunk in upstream_response.content.iter_any():
                                await resp.write(chunk)

                                # Try to parse for logging
                                try:
                                    chunk_text = chunk.decode('utf-8')
                                    # Extract JSON from SSE format
                                    for line in chunk_text.split('\n'):
                                        if line.startswith('data: '):
                                            chunk_json = json.loads(line[6:])
                                            chunks_buffer.append(chunk_json)
                                except:
                                    pass

                        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                            logger.debug("Client disconnected during streaming")
                        except Exception as e:
                            logger.error(f"Error during streaming: {e}", exc_info=True)
                        finally:
                            try:
                                await resp.write_eof()
                            except Exception:
                                pass
                            # Log to Phoenix
                            if chunks_buffer:
                                reassembled = reassemble_streaming_response(chunks_buffer)
                                span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps(reassembled))
                                span.set_attribute(
                                    "llm.token_count.total",
                                    reassembled.get("usageMetadata", {}).get("totalTokenCount", 0),
                                )

                        return resp
                    else:
                        # Non-streaming response
                        response_body = await upstream_response.read()
                        response_json = json.loads(response_body) if response_body else {}

                        # Log response
                        span.set_attribute(SpanAttributes.OUTPUT_VALUE, response_body.decode('utf-8'))
                        span.set_attribute(
                            "llm.token_count.total",
                            response_json.get("usageMetadata", {}).get("totalTokenCount", 0),
                        )

                        return web.Response(
                            status=upstream_response.status,
                            body=response_body,
                            headers=dict(upstream_response.headers),
                        )

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"Error: {e}")
            return web.Response(status=500, text=str(e))


async def handle_list_models(request):
    """Pure pass-through HTTP proxy for listing models"""
    # Build upstream URL preserving query parameters
    upstream_base = GEMINI_BASE_URL or "https://generativelanguage.googleapis.com"
    upstream_url = f"{upstream_base}{request.rel_url}"

    # Start Span
    with tracer.start_as_current_span(
        "gemini_list_models", kind=trace.SpanKind.SERVER
    ) as span:
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.url", request.path)

        try:
            # Forward request to upstream API
            async with aiohttp.ClientSession() as session:
                headers = sanitize_headers(request)
                async with session.get(upstream_url, headers=headers) as upstream_response:
                    response_body = await upstream_response.read()

                    # Log response
                    span.set_attribute(SpanAttributes.OUTPUT_VALUE, response_body.decode('utf-8'))

                    return web.Response(
                        status=upstream_response.status,
                        body=response_body,
                        headers=dict(upstream_response.headers),
                    )

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"Error: {e}")
            return web.Response(status=500, text=str(e))


async def handle_anthropic_request(request):
    """Pass-through proxy for Anthropic's API surface."""
    # Read and parse body first to check model
    body = await request.read()
    body_text = body.decode("utf-8", errors="ignore") if body else ""

    # Attempt to capture model information for tracing
    model_name = None
    parsed_body: Any = None
    if body:
        try:
            parsed_body = json.loads(body)
        except json.JSONDecodeError:
            parsed_body = None
    if isinstance(parsed_body, dict):
        model_name = parsed_body.get("model")

    # IMPORTANT: Check if this is actually an OpenAI model that was sent to the wrong endpoint
    if is_openai_model(model_name):
        logger.info(f"Redirecting {model_name} from Anthropic handler to OpenAI handler")
        # Route to OpenAI instead - need to transform the request for OpenAI format
        upstream_base = OPENAI_UPSTREAM_BASE or DEFAULT_OPENAI_API_BASE
        # OpenAI uses /v1/chat/completions, not /v1/messages
        upstream_url = f"{upstream_base}/v1/chat/completions"
        span_name = "openai_request"
    else:
        upstream_base = ANTHROPIC_UPSTREAM_BASE or DEFAULT_ANTHROPIC_API_BASE
        upstream_url = f"{upstream_base}{request.rel_url}"
        span_name = "anthropic_request"

    with tracer.start_as_current_span(
        span_name, kind=trace.SpanKind.SERVER
    ) as span:
        if model_name:
            span.set_attribute("llm.model", model_name)
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.url", request.path)
        span.set_attribute(SpanAttributes.INPUT_VALUE, body_text)

        try:
            async with aiohttp.ClientSession() as session:
                headers = sanitize_headers(request)

                # Inject Anthropic API key if configured and not using OpenAI
                if ANTHROPIC_API_KEY and not is_openai_model(model_name):
                    headers["x-api-key"] = ANTHROPIC_API_KEY
                    headers["anthropic-version"] = "2023-06-01"
                    logger.debug(f"Injecting Anthropic API key for model {model_name}")

                async with session.request(
                    request.method,
                    upstream_url,
                    data=body if body else None,
                    headers=headers,
                ) as upstream_response:
                    content_type = upstream_response.headers.get("Content-Type", "")
                    if content_type.lower().startswith("text/event-stream"):
                        response_headers = {
                            key: value
                            for key, value in upstream_response.headers.items()
                            if key.lower()
                            not in ["content-length", "transfer-encoding", "content-encoding"]
                        }

                        resp = web.StreamResponse(
                            status=upstream_response.status,
                            headers=response_headers,
                        )
                        await resp.prepare(request)

                        chunks_buffer: List[str] = []
                        try:
                            async for chunk in upstream_response.content.iter_any():
                                await resp.write(chunk)
                                try:
                                    chunks_buffer.append(chunk.decode("utf-8"))
                                except UnicodeDecodeError:
                                    pass
                        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                            logger.debug("Client disconnected during Anthropic streaming")
                        except Exception as stream_error:
                            logger.error(
                                f"Error during Anthropic streaming: {stream_error}",
                                exc_info=True,
                            )
                        finally:
                            try:
                                await resp.write_eof()
                            except Exception:
                                pass
                            if chunks_buffer:
                                span.set_attribute(
                                    SpanAttributes.OUTPUT_VALUE, "".join(chunks_buffer)
                                )

                        return resp

                    response_body = await upstream_response.read()
                    response_text = response_body.decode("utf-8", errors="ignore")
                    span.set_attribute(SpanAttributes.OUTPUT_VALUE, response_text)

                    try:
                        response_json = json.loads(response_body)
                    except json.JSONDecodeError:
                        response_json = None

                    if isinstance(response_json, dict):
                        usage = response_json.get("usage") or {}
                        total_tokens = usage.get("total_tokens")
                        if total_tokens is None and usage:
                            total_tokens = usage.get("input_tokens", 0) + usage.get(
                                "output_tokens", 0
                            )
                        if total_tokens:
                            span.set_attribute("llm.token_count.total", total_tokens)

                    return web.Response(
                        status=upstream_response.status,
                        body=response_body,
                        headers=dict(upstream_response.headers),
                    )
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"Anthropic proxy error: {e}")
            return web.Response(status=500, text=str(e))


async def handle_openai_request(request):
    """Pass-through proxy for OpenAI's API surface."""
    # Read and parse body first to check model
    body = await request.read()
    body_text = body.decode("utf-8", errors="ignore") if body else ""

    # Attempt to capture model information for tracing
    model_name = None
    is_streaming = False
    parsed_body: Any = None
    if body:
        try:
            parsed_body = json.loads(body)
        except json.JSONDecodeError:
            parsed_body = None
    if isinstance(parsed_body, dict):
        model_name = parsed_body.get("model")
        is_streaming = parsed_body.get("stream", False)

    # Debug logging - keep minimal at info level to avoid leaking tokens
    logger.info(f"OpenAI handler: path={request.path}, model={model_name}")
    logger.debug(f"Request headers: {dict(request.headers)}")
    logger.debug(f"Request body (first 500 chars): {body_text[:500]}")

    # IMPORTANT: Check if this is actually an Anthropic model that was sent to the wrong endpoint
    if is_anthropic_model(model_name):
        logger.info(f"Redirecting {model_name} from OpenAI handler to Anthropic handler")
        # Route to Anthropic instead
        upstream_base = ANTHROPIC_UPSTREAM_BASE or DEFAULT_ANTHROPIC_API_BASE
        # Anthropic uses /v1/messages, not /v1/chat/completions
        upstream_url = f"{upstream_base}/v1/messages"
        span_name = "anthropic_request"
        is_chatgpt_request = False
    else:
        # Detect ChatGPT/Codex auth tokens (bearer from ChatGPT login)
        is_chatgpt_request = (
            request.headers.get("chatgpt-account-id")
            or request.headers.get("originator") == "codex_cli_rs"
            or "codex" in request.headers.get("User-Agent", "").lower()
        )

        if is_chatgpt_request:
            # Codex ChatGPT tokens expect the ChatGPT backend codex endpoint, not api.openai.com
            chatgpt_base = "https://chatgpt.com/backend-api/codex"
            query = request.rel_url.query_string
            upstream_url = f"{chatgpt_base}/responses"
            if query:
                upstream_url = f"{upstream_url}?{query}"
            logger.info(f"Detected ChatGPT/Codex auth, routing to {upstream_url}")
            span_name = "chatgpt_request"
        else:
            upstream_base = OPENAI_UPSTREAM_BASE or DEFAULT_OPENAI_API_BASE
            upstream_url = f"{upstream_base}{request.rel_url}"
            span_name = "openai_request"

    logger.info(f"Forwarding to upstream URL: {upstream_url}")

    with tracer.start_as_current_span(
        span_name, kind=trace.SpanKind.SERVER
    ) as span:
        if model_name:
            span.set_attribute("llm.model", model_name)
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.url", request.path)
        span.set_attribute(SpanAttributes.INPUT_VALUE, body_text)

        try:
            async with aiohttp.ClientSession() as session:
                headers = sanitize_headers(request)

                # Inject OpenAI API key if configured and not using Anthropic or ChatGPT auth
                if OPENAI_API_KEY and not (is_anthropic_model(model_name) or is_chatgpt_request):
                    headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"
                    logger.debug(f"Injecting OpenAI API key for model {model_name}")

                async with session.request(
                    request.method,
                    upstream_url,
                    data=body if body else None,
                    headers=headers,
                ) as upstream_response:
                    content_type = upstream_response.headers.get("Content-Type", "")
                    if is_streaming or content_type.lower().startswith("text/event-stream"):
                        response_headers = {
                            key: value
                            for key, value in upstream_response.headers.items()
                            if key.lower()
                            not in ["content-length", "transfer-encoding", "content-encoding"]
                        }

                        resp = web.StreamResponse(
                            status=upstream_response.status,
                            headers=response_headers,
                        )
                        await resp.prepare(request)

                        chunks_buffer: List[str] = []
                        try:
                            async for chunk in upstream_response.content.iter_any():
                                await resp.write(chunk)
                                try:
                                    chunks_buffer.append(chunk.decode("utf-8"))
                                except UnicodeDecodeError:
                                    pass
                        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                            logger.debug("Client disconnected during OpenAI streaming")
                        except Exception as stream_error:
                            logger.error(
                                f"Error during OpenAI streaming: {stream_error}",
                                exc_info=True,
                            )
                        finally:
                            try:
                                await resp.write_eof()
                            except Exception:
                                pass
                            if chunks_buffer:
                                full_stream = "".join(chunks_buffer)
                                span.set_attribute(SpanAttributes.OUTPUT_VALUE, full_stream)

                                # Try to extract token usage from the final chunk
                                try:
                                    for line in full_stream.split('\n'):
                                        if line.startswith('data: ') and line[6:].strip() != '[DONE]':
                                            chunk_json = json.loads(line[6:])
                                            if isinstance(chunk_json, dict) and "usage" in chunk_json:
                                                usage = chunk_json["usage"]
                                                total_tokens = usage.get("total_tokens")
                                                if total_tokens:
                                                    span.set_attribute("llm.token_count.total", total_tokens)
                                                break
                                except Exception:
                                    pass

                        return resp

                    response_body = await upstream_response.read()
                    response_text = response_body.decode("utf-8", errors="ignore")
                    span.set_attribute(SpanAttributes.OUTPUT_VALUE, response_text)

                    # Log response details at debug to avoid noise/leaks
                    logger.debug(f"Upstream response status: {upstream_response.status}")
                    logger.debug(f"Upstream response body (first 500 chars): {response_text[:500]}")

                    try:
                        response_json = json.loads(response_body)
                    except json.JSONDecodeError:
                        response_json = None

                    if isinstance(response_json, dict):
                        usage = response_json.get("usage") or {}
                        total_tokens = usage.get("total_tokens")
                        if total_tokens:
                            span.set_attribute("llm.token_count.total", total_tokens)

                    return web.Response(
                        status=upstream_response.status,
                        body=response_body,
                        headers=dict(upstream_response.headers),
                    )
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"OpenAI proxy error: {e}")
            return web.Response(status=500, text=str(e))


@web.middleware
async def logging_middleware(request, handler):
    """Log incoming requests"""
    logger.info(f"{request.method} {request.path}")

    try:
        response = await handler(request)
        logger.info(f"{response.status} {request.method} {request.path}")
        return response
    except Exception as e:
        logger.error(f"Error handling {request.method} {request.path}: {e}")
        raise


async def init_app():
    app = web.Application(middlewares=[logging_middleware], client_max_size=100*1024*1024)

    # Simple liveness endpoint for container health checks
    async def handle_health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", handle_health)

    # Gemini routes (v1beta)
    app.router.add_get("/v1beta/models", handle_list_models)
    app.router.add_post(
        "/v1beta/models/{model}:generateContent", handle_generate_content
    )
    app.router.add_post(
        "/v1beta/models/{model}:streamGenerateContent", handle_generate_content
    )

    # OpenAI routes (must come before Anthropic catch-all)
    app.router.add_route("*", "/v1/chat/completions", handle_openai_request)
    app.router.add_route("*", "/v1/completions", handle_openai_request)
    app.router.add_route("*", "/v1/responses", handle_openai_request)  # ChatGPT codex_cli uses this
    app.router.add_route("*", "/v1/embeddings", handle_openai_request)
    app.router.add_route("*", "/v1/models", handle_openai_request)
    app.router.add_route("*", "/v1/images/generations", handle_openai_request)
    app.router.add_route("*", "/v1/images/edits", handle_openai_request)
    app.router.add_route("*", "/v1/images/variations", handle_openai_request)
    app.router.add_route("*", "/v1/audio/transcriptions", handle_openai_request)
    app.router.add_route("*", "/v1/audio/translations", handle_openai_request)
    app.router.add_route("*", "/v1/audio/speech", handle_openai_request)

    # Anthropic routes (specific endpoints)
    app.router.add_route("*", "/v1/messages", handle_anthropic_request)
    app.router.add_route("*", "/v1/messages/{tail:.*}", handle_anthropic_request)
    app.router.add_route("*", "/v1/complete", handle_anthropic_request)
    return app


if __name__ == "__main__":
    # Use port 8081 as 8080 was taken
    web.run_app(init_app(), port=8082)
