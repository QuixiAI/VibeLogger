#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "aiohttp",
#     "brotli",
#     "arize-phoenix-otel",
#     "opentelemetry-api",
#     "opentelemetry-sdk",
#     "pyyaml",
# ]
# ///

import asyncio
import json
import logging
import yaml
from typing import Callable, Dict, Any, List
from aiohttp import web
import aiohttp
from yarl import URL

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from openinference.semconv.trace import SpanAttributes

# Configure logging (timestamps matter: stream errors must be correlatable
# with client-side failures)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
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
META_UPSTREAM_BASE = config.get("meta_upstream_base") or "https://api.meta.ai"
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


# aiohttp's client decompresses upstream bodies, so these entity headers
# describe bytes we no longer have; forwarding them corrupts the response.
ENTITY_HEADERS = {"content-length", "content-encoding"}


def filter_response_headers(headers) -> Dict[str, str]:
    """Headers safe to relay downstream after aiohttp (de)framing."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower() not in ENTITY_HEADERS
    }


# No total/sock_read cap: aiohttp's 300s default kills long LLM streams
# (extended thinking, long generations) mid-response. The downstream
# client and the upstream API both enforce their own timeouts.
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30)
UPSTREAM_SESSION_KEY = web.AppKey("upstream_session", aiohttp.ClientSession)


def _make_upstream_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=UPSTREAM_TIMEOUT,
        connector=aiohttp.TCPConnector(ttl_dns_cache=300, keepalive_timeout=75),
    )


async def _upstream_session_ctx(app: web.Application):
    """Own the shared upstream session for the app's lifetime."""
    session = app.get(UPSTREAM_SESSION_KEY)
    if session is None or session.closed:
        session = _make_upstream_session()
        app[UPSTREAM_SESSION_KEY] = session
    yield
    if not session.closed:
        await session.close()


def get_upstream_session(request: web.Request) -> aiohttp.ClientSession:
    """One pooled session per app: connection reuse, DNS caching, keep-alive.

    A fresh ClientSession per request forces a new DNS lookup, TCP and TLS
    handshake for every API call, and discards keep-alive connections.
    Created at startup by _upstream_session_ctx; the lazy fallback covers
    handlers invoked on an app that was never started.
    """
    session = request.app.get(UPSTREAM_SESSION_KEY)
    if session is None or session.closed:
        session = _make_upstream_session()
        request.app[UPSTREAM_SESSION_KEY] = session
    return session


def _is_client_disconnect(request: web.Request, error: BaseException) -> bool:
    """True when the downstream client went away, not the upstream API."""
    if isinstance(error, (ConnectionResetError, BrokenPipeError,
                          aiohttp.ClientConnectionResetError)):
        return True
    if isinstance(error, RuntimeError) and "closing transport" in str(error):
        return True
    transport = request.transport
    return transport is None or transport.is_closing()


def _abort_response(request: web.Request, resp: web.StreamResponse) -> None:
    """Abort a started streaming response without a terminating chunk.

    StreamResponse.force_close() only disables keep-alive; the server would
    still send the final chunk when the handler returns, making a truncated
    stream look complete. Aborting the transport and marking EOF as sent
    (aiohttp has no public API for this) gives the client a visibly broken
    connection instead.
    """
    resp.force_close()
    resp._eof_sent = True
    transport = request.transport
    if transport is not None and not transport.is_closing():
        transport.abort()


async def stream_upstream_response(
    request: web.Request,
    upstream_response: aiohttp.ClientResponse,
    span: Any,
    provider: str,
    record_chunks: Callable[[List[bytes]], None],
) -> web.StreamResponse:
    """Relay a streaming (SSE) upstream response to the client.

    A clean EOF is sent only when the upstream body completed. If the
    upstream connection dies mid-stream, the downstream transport is
    aborted instead: the client must see a broken connection (and retry),
    never a truncated stream that looks complete. Client disconnects are
    expected and logged without an error.
    """
    resp = web.StreamResponse(
        status=upstream_response.status,
        headers=filter_response_headers(upstream_response.headers),
    )
    await resp.prepare(request)
    chunks: List[bytes] = []
    try:
        async for chunk in upstream_response.content.iter_any():
            await resp.write(chunk)
            chunks.append(chunk)
        await resp.write_eof()
    except asyncio.CancelledError:
        _abort_response(request, resp)
        raise
    except Exception as error:
        if _is_client_disconnect(request, error):
            logger.info(f"Client disconnected during {provider} streaming")
        else:
            span.record_exception(error)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"Error during {provider} streaming: {error}", exc_info=True)
        _abort_response(request, resp)
    finally:
        if chunks:
            try:
                record_chunks(chunks)
            except Exception as record_error:
                logger.error(f"Error recording {provider} stream: {record_error}")
    return resp


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
            session = get_upstream_session(request)
            headers = sanitize_headers(request)

            async with session.post(
                upstream_url,
                data=body,
                headers=headers,
            ) as upstream_response:

                if is_streaming:
                    def record_chunks(chunks: List[bytes]) -> None:
                        parsed = []
                        text = b"".join(chunks).decode("utf-8", errors="ignore")
                        for line in text.split("\n"):
                            if line.startswith("data: "):
                                try:
                                    parsed.append(json.loads(line[6:]))
                                except ValueError:
                                    pass
                        if parsed:
                            reassembled = reassemble_streaming_response(parsed)
                            span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps(reassembled))
                            span.set_attribute(
                                "llm.token_count.total",
                                reassembled.get("usageMetadata", {}).get("totalTokenCount", 0),
                            )

                    return await stream_upstream_response(
                        request, upstream_response, span, "Gemini", record_chunks
                    )

                # Non-streaming response
                response_body = await upstream_response.read()
                response_json = json.loads(response_body) if response_body else {}

                span.set_attribute(
                    SpanAttributes.OUTPUT_VALUE,
                    response_body.decode("utf-8", errors="ignore"),
                )
                span.set_attribute(
                    "llm.token_count.total",
                    response_json.get("usageMetadata", {}).get("totalTokenCount", 0),
                )

                return web.Response(
                    status=upstream_response.status,
                    body=response_body,
                    headers=filter_response_headers(upstream_response.headers),
                )

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"Gemini proxy error: {e}", exc_info=True)
            return web.Response(status=502, text="Gemini upstream request failed")


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
            session = get_upstream_session(request)
            headers = sanitize_headers(request)
            async with session.get(upstream_url, headers=headers) as upstream_response:
                response_body = await upstream_response.read()

                span.set_attribute(
                    SpanAttributes.OUTPUT_VALUE,
                    response_body.decode("utf-8", errors="ignore"),
                )

                return web.Response(
                    status=upstream_response.status,
                    body=response_body,
                    headers=filter_response_headers(upstream_response.headers),
                )

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"Gemini proxy error: {e}", exc_info=True)
            return web.Response(status=502, text="Gemini upstream request failed")


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
            session = get_upstream_session(request)
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
                    def record_chunks(chunks: List[bytes]) -> None:
                        span.set_attribute(
                            SpanAttributes.OUTPUT_VALUE,
                            b"".join(chunks).decode("utf-8", errors="replace"),
                        )

                    return await stream_upstream_response(
                        request, upstream_response, span, "Anthropic", record_chunks
                    )

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
                    headers=filter_response_headers(upstream_response.headers),
                )
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"Anthropic proxy error: {e}", exc_info=True)
            return web.Response(status=502, text="Anthropic upstream request failed")


async def handle_meta_request(request):
    """Forward Muse traffic to Meta, preserving its credentials and wire format."""
    body = await request.read()
    try:
        payload = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}

    # The prefix selects Meta explicitly; model names do not select providers.
    # Muse's model discovery uses /muse-code/models at the base URL's origin.
    path = request.raw_path
    if path.startswith("/meta/"):
        path = path[len("/meta"):]
    upstream_url = f"{META_UPSTREAM_BASE.rstrip('/')}{path}"

    with tracer.start_as_current_span("meta_request", kind=trace.SpanKind.SERVER) as span:
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.url", request.path)
        span.set_attribute(SpanAttributes.INPUT_VALUE, body.decode("utf-8", errors="replace"))
        if isinstance(payload, dict) and payload.get("model"):
            span.set_attribute("llm.model", payload["model"])

        def record_output(output):
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, output)
            # Responses API usage is nested in response.completed SSE events.
            candidates = []
            try:
                candidates.append(json.loads(output))
            except ValueError:
                for line in output.splitlines():
                    if line.startswith("data:"):
                        try:
                            candidates.append(json.loads(line[5:].strip()))
                        except ValueError:
                            continue
            for event in candidates:
                if not isinstance(event, dict):
                    continue
                response = event.get("response")
                result = response if isinstance(response, dict) else event
                usage = result.get("usage")
                if isinstance(usage, dict):
                    total = usage.get("total_tokens")
                    if total is None:
                        total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    if total is not None:
                        span.set_attribute("llm.token_count.total", total)

        try:
            session = get_upstream_session(request)
            async with session.request(
                request.method,
                URL(upstream_url, encoded=True),
                data=body if body else None,
                headers=sanitize_headers(request),
                allow_redirects=False,
            ) as upstream_response:
                if upstream_response.headers.get("Content-Type", "").lower().startswith("text/event-stream"):
                    def record_chunks(chunks: List[bytes]) -> None:
                        record_output(b"".join(chunks).decode("utf-8", errors="replace"))

                    return await stream_upstream_response(
                        request, upstream_response, span, "Meta", record_chunks
                    )

                output = await upstream_response.read()
                record_output(output.decode("utf-8", errors="replace"))
                return web.Response(
                    status=upstream_response.status,
                    body=output,
                    headers=filter_response_headers(upstream_response.headers),
                )
        except Exception as error:
            span.record_exception(error)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error("Meta proxy error: %s", error, exc_info=True)
            return web.Response(status=502, text="Meta upstream request failed")


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
            # Preserve the path after /v1 (e.g., /v1/responses/compact -> /responses/compact)
            path_suffix = request.path[3:]  # Strip "/v1"
            query = request.rel_url.query_string
            upstream_url = f"{chatgpt_base}{path_suffix}"
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
            session = get_upstream_session(request)
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
                    def record_chunks(chunks: List[bytes]) -> None:
                        full_stream = b"".join(chunks).decode("utf-8", errors="replace")
                        span.set_attribute(SpanAttributes.OUTPUT_VALUE, full_stream)
                        # Token usage, when present, arrives in a streamed chunk.
                        for line in full_stream.split("\n"):
                            if not (line.startswith("data: ") and line[6:].strip() != "[DONE]"):
                                continue
                            try:
                                chunk_json = json.loads(line[6:])
                            except ValueError:
                                continue
                            if isinstance(chunk_json, dict) and isinstance(chunk_json.get("usage"), dict):
                                total_tokens = chunk_json["usage"].get("total_tokens")
                                if total_tokens:
                                    span.set_attribute("llm.token_count.total", total_tokens)
                                break

                    return await stream_upstream_response(
                        request, upstream_response, span, "OpenAI", record_chunks
                    )

                response_body = await upstream_response.read()
                response_text = response_body.decode("utf-8", errors="ignore")
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, response_text)

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
                    headers=filter_response_headers(upstream_response.headers),
                )
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            logger.error(f"OpenAI proxy error: {e}", exc_info=True)
            return web.Response(status=502, text="OpenAI upstream request failed")


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

    app.cleanup_ctx.append(_upstream_session_ctx)

    # Simple liveness endpoint for container health checks
    async def handle_health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", handle_health)

    # Explicit provider prefix keeps Muse and OpenAI Responses traffic separate.
    app.router.add_route("*", "/meta/v1/{tail:.*}", handle_meta_request)
    app.router.add_route("*", "/meta/muse-code/{tail:.*}", handle_meta_request)
    app.router.add_route("*", "/muse-code/{tail:.*}", handle_meta_request)

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
    app.router.add_route("*", "/v1/responses/{tail:.*}", handle_openai_request)  # Codex compact, etc.
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
    # handler_cancellation: when a client disconnects mid-stream, cancel the
    # handler so the upstream generation is abandoned instead of relayed
    # into a dead socket.
    web.run_app(init_app(), port=8082, handler_cancellation=True)
