"""Durable, explicit coordination of Immich's two metadata queues.

Callers hold the state directory's writer lock for this entire lifecycle.
"""
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager

from .immich_client import ProcessingCancelled

QUEUES = ("metadataExtraction", "sidecar")
STATE_FILE = "cleanup-queues.json"


class QueueMaintenanceError(RuntimeError):
    """Task-level error: never commit a successful asset while queues are uncertain."""


class CleanupQueues:
    poll_interval = 0.25

    def __init__(self, client, settings, progress, cancelled):
        self.client = client
        self.settings = settings
        self.progress = progress
        self.cancelled = cancelled
        self.path = settings.state_dir / STATE_FILE
        self.server = hashlib.sha256(settings.immich_base_url.encode()).hexdigest()

    def _sync_directory(self):
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        self._sync_directory()

    def _load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or type(state.get("version")) is not int
                    or state["version"] != 1 or state.get("server") != self.server
                    or not isinstance(state.get("queues"), dict) or set(state["queues"]) != set(QUEUES)):
                raise ValueError("Invalid queue recovery record")
            for queue in state["queues"].values():
                if (not isinstance(queue, dict) or type(queue.get("isPaused")) is not bool
                        or type(queue.get("failed")) is not int or queue["failed"] < 0):
                    raise ValueError("Invalid original queue state")
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise QueueMaintenanceError("清理队列恢复记录损坏或服务地址不匹配；请核对 cleanup-queues.json") from error
        return state

    def _phase(self, state, name, message):
        state["phase"] = name
        self._save(state)
        self.progress.log(message)

    def _read(self):
        return {name: self.client.get_cleanup_queue(name) for name in QUEUES}

    def preflight(self):
        # GET both queues before any state/remote mutation. The confirmed pause
        # responses below establish update permission before any tag deletion.
        return self._read()

    def _pause(self):
        for name in QUEUES:
            self.client.set_cleanup_queue_paused(name, True)

    def _wait(self, names, *, active_only=False, cancellable=False, paused=()):
        deadline = time.monotonic() + self.settings.cleanup_queue_timeout
        consecutive = 0
        with self.progress.operation("wait_queues"):
            while True:
                if cancellable and self.cancelled.is_set():
                    raise ProcessingCancelled()
                queues = self._read()
                if any(queues[n]["isPaused"] != (n in paused) for n in QUEUES):
                    raise QueueMaintenanceError("维护期间队列状态被更改，请停止其他队列控制任务后恢复")
                counts = ("active",) if active_only else ("active", "waiting", "delayed", "paused")
                quiet = all(not any(queues[n]["statistics"][k] for k in counts) for n in names)
                consecutive = consecutive + 1 if quiet else 0
                self.progress.update(queue_counts={n: queues[n]["statistics"] for n in QUEUES})
                # Spaced empty reads also cover follow-up jobs enqueued on completion.
                if consecutive >= 2:
                    return queues
                if time.monotonic() >= deadline:
                    raise QueueMaintenanceError(f"后台队列等待超过 {self.settings.cleanup_queue_timeout:g} 秒")
                time.sleep(self.poll_interval)

    @contextmanager
    def paused(self):
        if self.cancelled.is_set():
            raise ProcessingCancelled()
        if self.path.exists():
            raise QueueMaintenanceError("存在未恢复的清理队列记录，请先恢复")
        queues = self.preflight()
        state = {"version": 1, "server": self.server, "phase": "pausing",
                 "queues": {n: {"isPaused": q["isPaused"], "failed": q["statistics"]["failed"]}
                            for n, q in queues.items()}}
        # Intent reaches disk before either pause, including uncertain HTTP responses.
        self._save(state)
        try:
            self._pause()
            self._wait(QUEUES, active_only=True, cancellable=True, paused=QUEUES)
            self._phase(state, "cleaning", "后台队列已暂停，开始本张标签清理")
            yield
        finally:
            # Restoration ignores graceful cancellation; SIGKILL leaves durable intent.
            self.recover()

    def recover(self):
        if not self.path.exists():
            return False
        state = self._load()
        try:
            # On restart either queue may already be running. Quiesce BOTH again
            # before releasing sidecar so old extraction cannot interleave with it.
            self._phase(state, "restoring", "恢复后台队列：先完成 XMP 写入，再提取元数据")
            self._pause()
            self._wait(QUEUES, active_only=True, paused=QUEUES)
            self._phase(state, "sidecar", "等待 XMP 写入完成")
            self.client.set_cleanup_queue_paused("sidecar", False)
            self._wait(("sidecar",), paused=("metadataExtraction",))
            self._phase(state, "metadata", "等待元数据提取完成")
            self.client.set_cleanup_queue_paused("metadataExtraction", False)
            final = self._wait(QUEUES)
            failed = [n for n in QUEUES if final[n]["statistics"]["failed"] > state["queues"][n]["failed"]]
            self._phase(state, "original", "恢复队列原始暂停状态")
            for name in QUEUES:
                self.client.set_cleanup_queue_paused(name, state["queues"][name]["isPaused"])
            self.path.unlink()
            self._sync_directory()
            self.progress.update(queue_counts=None)
        except Exception as error:
            raise QueueMaintenanceError(
                f"后台队列恢复未完成（{error}）；保留 cleanup-queues.json，"
                "请执行 --restore-cleanup-queues 或续跑维护命令"
            ) from error
        if failed:
            raise QueueMaintenanceError("后台任务出现新增失败，队列已恢复但本张未确认：" + ", ".join(failed))
        return True


def restore_cleanup_queues(settings):
    from .immich_client import ImmichClient
    from .progress import ProgressState
    from .state import writer_lock

    if not settings.cleanup_admin_api_key.strip():
        raise ValueError("队列恢复需要 CLEANUP_ADMIN_API_KEY")
    progress = ProgressState(settings)
    with writer_lock(settings.state_dir):
        with ImmichClient(settings, {"name": "cleanup-admin", "api_key": settings.cleanup_admin_api_key}) as client:
            client.progress = progress
            with progress.reporting():
                restored = CleanupQueues(client, settings, progress, threading.Event()).recover()
                progress.log("清理维护队列已恢复" if restored else "没有需要恢复的清理维护队列")
