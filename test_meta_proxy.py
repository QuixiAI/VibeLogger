import gzip
import unittest
from unittest.mock import MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

# Import with tracing disabled so these local tests never contact Phoenix.
with patch("yaml.safe_load", return_value={}):
    import proxy


class MetaProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.response_body = b'{"usage":{"input_tokens":3,"output_tokens":4}}'
        self.response_status = 200
        self.response_headers = {"Content-Type": "application/json"}
        self.stream_chunks = None

        async def upstream(request):
            self.received.append((request.method, request.raw_path,
                                  dict(request.headers), await request.read()))
            if self.stream_chunks is not None:
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                for chunk in self.stream_chunks:
                    await response.write(chunk)
                await response.write_eof()
                return response
            return web.Response(status=self.response_status, body=self.response_body,
                                headers=self.response_headers)

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{tail:.*}", upstream)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        self.span = MagicMock()
        tracer = MagicMock()
        tracer.start_as_current_span.return_value.__enter__.return_value = self.span
        self.patches = [
            patch.object(proxy, "META_UPSTREAM_BASE", str(self.upstream.make_url("/"))),
            patch.object(proxy, "tracer", tracer),
            patch.object(proxy, "OPENAI_API_KEY", "must-not-be-injected"),
            patch.object(proxy, "ANTHROPIC_API_KEY", "must-not-be-injected"),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(TestServer(await proxy.init_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        for item in reversed(self.patches):
            item.stop()

    async def test_responses_preserve_body_auth_query_and_session(self):
        body = b'{"model":"claude-probe","input":"hello","stream":false}'
        response = await self.client.post(
            URL("/meta/v1/responses?probe=a%2Fb&probe=c", encoded=True),
            data=body, headers={"Authorization": "Bearer test-muse-key",
                                "x-client-id": "test-client",
                                "x-tbh-session-id": "test-session",
                                "Content-Type": "application/json"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.response_body)
        method, path, headers, forwarded_body = self.received[0]
        self.assertEqual((method, path), ("POST", "/v1/responses?probe=a%2Fb&probe=c"))
        self.assertEqual(forwarded_body, body)
        self.assertEqual(headers["Authorization"], "Bearer test-muse-key")
        self.assertEqual(headers["x-client-id"], "test-client")
        self.assertEqual(headers["x-tbh-session-id"], "test-session")
        self.assertNotIn("x-api-key", headers)
        self.span.set_attribute.assert_any_call("llm.token_count.total", 7)

    async def test_origin_relative_model_discovery(self):
        response = await self.client.get("/muse-code/models")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.received[0][:2], ("GET", "/muse-code/models"))

    async def test_interactive_config_strips_provider_prefix(self):
        response = await self.client.get("/meta/muse-code/config?channel=public")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.received[0][:2], ("GET", "/muse-code/config?channel=public"))

    async def test_stream_is_unchanged_and_completed_usage_is_traced(self):
        body = 'data: {"type":"response.output_text.delta","delta":"café"}\n\n'.encode()
        split = body.index(b'\xc3') + 1
        completed = b'data: {"type":"response.completed","response":{"usage":{"total_tokens":12}}}\n\n'
        self.stream_chunks = [body[:split], body[split:], completed, b'data: [DONE]\n\n']
        response = await self.client.post("/meta/v1/responses", json={"stream": True})
        output = await response.read()
        self.assertEqual(output, b"".join(self.stream_chunks))
        self.assertEqual(response.content_type, "text/event-stream")
        self.span.set_attribute.assert_any_call(proxy.SpanAttributes.OUTPUT_VALUE, output.decode())
        self.span.set_attribute.assert_any_call("llm.token_count.total", 12)

    async def test_compressed_upstream_error_preserves_status_and_decoded_body(self):
        self.response_body = gzip.compress(b'{"error":"invalid key"}')
        self.response_headers["Content-Encoding"] = "gzip"
        self.response_status = 401
        response = await self.client.post("/meta/v1/responses", json={})
        self.assertEqual(response.status, 401)
        self.assertEqual(await response.read(), b'{"error":"invalid key"}')
        self.assertNotIn("Content-Encoding", response.headers)

    async def test_existing_provider_routes_keep_their_handlers(self):
        app = self.client.server.app
        handlers = {resource.canonical: route.handler
                    for resource in app.router.resources() for route in resource}
        self.assertIs(handlers["/v1/messages"], proxy.handle_anthropic_request)
        self.assertIs(handlers["/v1/responses"], proxy.handle_openai_request)
        self.assertIs(handlers["/v1beta/models"], proxy.handle_list_models)


if __name__ == "__main__":
    unittest.main()
