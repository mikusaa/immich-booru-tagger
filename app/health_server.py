"""Health checks use snapshots and never change the active account."""
import logging

from aiohttp import web
from . import __version__


class HealthServer:
    def __init__(self, processor):
        self.processor = processor
        self.app = web.Application()
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/metrics", self.metrics)
        self.app.router.add_get("/", self.info)
        self.runner = None
        self._healthy = True
        self.logger = logging.getLogger("health")

    async def health(self, request):
        metrics = self.processor.get_metrics()
        healthy = not bool(metrics["last_error"])
        if healthy != self._healthy:
            self._healthy = healthy
            self.logger.log(logging.INFO if healthy else logging.WARNING,
                            "[健康] %s", "状态已恢复" if healthy else "最近任务发生错误，请查看任务日志")
        return web.json_response({"status": "unhealthy" if metrics["last_error"] else "healthy",
                                  **metrics}, status=503 if metrics["last_error"] else 200)

    async def metrics(self, request):
        return web.json_response(self.processor.get_metrics())

    async def info(self, request):
        return web.json_response({"service": "immich-booru-tagger", "version": __version__,
                                  "endpoints": ["/health", "/metrics"]})

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.processor.settings.health_port).start()

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
