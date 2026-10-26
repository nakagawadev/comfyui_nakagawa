"""Drain mode: stop taking new prompts so the queue empties before a restart.

Off unless `drain: true` in comfyui_nakagawa.yaml (global or per instance).
The endpoints only answer loopback callers. State lives in memory, so a restart
always comes back accepting prompts.
"""
import time

LOOPBACK = ("127.0.0.1", "::1")
RETRY_AFTER = "30"


class Drain:
    def __init__(self, config, queue):
        self.config, self.queue = config, queue
        self.since = None

    def enabled(self):
        return bool(self.config.get().get("drain"))

    def active(self):
        return self.since is not None and self.enabled()

    def status(self):
        running, pending = self.queue.get_current_queue_volatile()
        return {"draining": self.active(), "since": self.since if self.active() else None,
                "running": len(running), "pending": len(pending)}


def install(server, config):
    from aiohttp import web

    drain = Drain(config, server.prompt_queue)

    @web.middleware
    async def reject_prompts(request, handler):
        if request.method == "POST" and request.path in ("/prompt", "/api/prompt") and drain.active():
            return web.json_response(
                {"error": {"type": "draining", "message": "This instance is draining; send the prompt to another one.",
                           "details": "", "extra_info": {}}, "node_errors": {}},
                status=503, headers={"Retry-After": RETRY_AFTER})
        return await handler(request)

    # Custom nodes load before the app starts, so the middleware list isn't frozen yet.
    server.app.middlewares.append(reject_prompts)

    def guard(request):
        if not drain.enabled():
            raise web.HTTPNotFound()
        if request.remote not in LOOPBACK:
            raise web.HTTPForbidden(text="drain endpoints only answer loopback callers")

    @server.routes.get("/nkg/drain")
    async def drain_status(request):
        guard(request)
        return web.json_response(drain.status())

    @server.routes.post("/nkg/drain")
    async def drain_on(request):
        guard(request)
        if drain.since is None:
            drain.since = time.time()
        return web.json_response(drain.status())

    @server.routes.delete("/nkg/drain")
    async def drain_off(request):
        guard(request)
        drain.since = None
        return web.json_response(drain.status())

    return drain
