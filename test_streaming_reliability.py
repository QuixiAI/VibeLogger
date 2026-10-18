import gzip
import unittest
from unittest.mock import MagicMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

with patch("yaml.safe_load", return_value={}):
    import proxy


class StreamingReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode = "stream"  # stream | truncate | gzip_json

        async def upstream(request):
            await request.read()
            if self.mode == "gzip_json":
                body = gzip.compress(b'{"type":"message","content":[]}')
                return web.Response(body=body, headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                })
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: {\"partial\":true}\n\n")
            if self.mode == "truncate":
                # Kill the socket mid-stream: no terminating chunk.
                request.transport.abort()
                return response
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{tail:.*}", upstream)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        base = str(self.upstream.make_url("/")).rstrip("/")

        tracer = MagicMock()
        tracer.start_as_current_span.return_value.__enter__.return_value = MagicMock()
        self.patches = [
            patch.object(proxy, "tracer", tracer),
            patch.object(proxy, "ANTHROPIC_UPSTREAM_BASE", base),
            patch.object(proxy, "OPENAI_UPSTREAM_BASE", base),
            patch.object(proxy, "GEMINI_BASE_URL", base),
            patch.object(proxy, "META_UPSTREAM_BASE", base),
            patch.object(proxy, "OPENAI_API_KEY", None),
            patch.object(proxy, "ANTHROPIC_API_KEY", None),
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

    PROVIDER_REQUESTS = [
        ("/v1/messages", {"model": "claude-test", "stream": True}),
        ("/v1/chat/completions", {"model": "gpt-test", "stream": True}),
        ("/v1beta/models/gemini-test:streamGenerateContent", {}),
        ("/meta/v1/responses", {"stream": True}),
    ]

    async def test_truncated_upstream_stream_is_not_delivered_as_clean_eof(self):
        self.mode = "truncate"
        for path, payload in self.PROVIDER_REQUESTS:
            with self.subTest(path=path):
                with self.assertRaises(
                    (aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError,
                     aiohttp.ClientConnectionError, ConnectionResetError)
                ):
                    response = await self.client.post(path, json=payload)
                    await response.read()

    async def test_complete_stream_passes_through_for_all_providers(self):
        for path, payload in self.PROVIDER_REQUESTS:
            with self.subTest(path=path):
                response = await self.client.post(path, json=payload)
                body = await response.read()
                self.assertEqual(response.status, 200)
                self.assertIn(b"[DONE]", body)

    async def test_non_streaming_gzip_body_is_decoded_with_fresh_headers(self):
        self.mode = "gzip_json"
        response = await self.client.post("/v1/messages", json={"model": "claude-test"})
        self.assertEqual(await response.read(), b'{"type":"message","content":[]}')
        self.assertNotIn("Content-Encoding", response.headers)

    def test_upstream_timeout_has_no_total_cap_for_long_streams(self):
        self.assertIsNone(proxy.UPSTREAM_TIMEOUT.total)


if __name__ == "__main__":
    unittest.main()
