"""Run one shared processor without blocking the health server."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter


class Scheduler:
    def __init__(self, processor):
        self.processor = processor
        self.settings = processor.settings
        self.stop_event = asyncio.Event()
        self.logger = logging.getLogger("scheduler")
        self.zone = ZoneInfo(self.settings.timezone)
        if not croniter.is_valid(self.settings.cron_schedule):
            raise ValueError("Invalid CRON_SCHEDULE")

    async def run_once(self, **kwargs):
        try:
            result = await asyncio.to_thread(self.processor.run, **kwargs)
            return 1 if result.failed else 0
        except Exception:
            self.logger.error("[异常] 定时任务执行失败，未完成队列保留｜%s", self.processor.last_error)
            return 1

    async def start(self, **kwargs):
        if not self.settings.enable_scheduler:
            return await self.run_once(**kwargs)
        resume = (self.settings.resume_on_startup
                  and await asyncio.to_thread(self.processor.has_pending_run, **kwargs))
        if resume or self.settings.run_on_startup:
            await self.run_once(**kwargs)
        while not self.stop_event.is_set():
            now = datetime.now(self.zone)
            next_run = croniter(self.settings.cron_schedule, now).get_next(datetime)
            self.processor.progress.update(next_run_at=next_run.isoformat(), current_asset_id=None, account=None)
            self.processor.progress.phase("waiting", f"等待下一轮｜{next_run.isoformat()}")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=max(0, next_run.timestamp() - now.timestamp()))
            except asyncio.TimeoutError:
                await self.run_once(**kwargs)
        return 0

    def stop(self):
        self.processor.cancelled.set()
        self.stop_event.set()
