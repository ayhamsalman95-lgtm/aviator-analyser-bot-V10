"""Small HTTP server for browser-side Aviator event ingestion.

Run with:
    python3 -m browser_collector.server

Set BROWSER_COLLECTOR_TOKEN in the environment. The endpoint accepts only
SmartFox extensionResponse snapshots produced by the browser collector.
"""
from __future__ import annotations

import os

from aiohttp import web

from aviator.browser_ingest import BrowserIngestor


MAX_BODY_BYTES = 512 * 1024
TOKEN = os.environ.get("BROWSER_COLLECTOR_TOKEN", "").strip()
INGESTOR = BrowserIngestor()


def _cors(response: web.StreamResponse) -> web.StreamResponse:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response


async def health(request: web.Request) -> web.Response:
    return _cors(web.json_response({"ok": True, "service": "browser-collector"}))


async def options(request: web.Request) -> web.Response:
    return _cors(web.Response(status=204))


async def ingest(request: web.Request) -> web.Response:
    if not TOKEN:
        return _cors(web.json_response({"error": "BROWSER_COLLECTOR_TOKEN is not configured"}, status=503))

    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {TOKEN}":
        return _cors(web.json_response({"error": "unauthorized"}, status=401))

    if request.content_length and request.content_length > MAX_BODY_BYTES:
        return _cors(web.json_response({"error": "payload too large"}, status=413))

    try:
        payload = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "invalid JSON"}, status=400))

    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return _cors(web.json_response({"error": "expected {events:[...]}"}, status=400))

    if len(events) > 500:
        return _cors(web.json_response({"error": "too many events"}, status=413))

    try:
        result = INGESTOR.ingest(events)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return _cors(web.json_response({
            "error": f"{type(exc).__name__}: {exc}",
        }, status=500))
    return _cors(web.json_response({"ok": True, **result}))


def create_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_get("/browser/health", health)
    app.router.add_options("/browser/events", options)
    app.router.add_post("/browser/events", ingest)
    return app


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    web.run_app(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
