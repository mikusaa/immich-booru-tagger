"""One thread-safe progress snapshot shared by logs and HTTP metrics."""
import copy
import logging
import threading
import time
from contextlib import contextmanager

from .task_store import utc_now


PHASES = {"idle": "空闲", "recovering": "恢复", "scanning": "扫描", "preparing_model": "模型",
          "processing": "处理", "stopping": "停止", "paused": "暂停", "completed": "完成",
          "error": "异常", "waiting": "调度"}
OPERATIONS = {"read_tags": "读取账号标签", "read_album": "读取相册成员", "search": "获取图片列表",
              "read_asset": "读取资产状态", "read_details": "补取图片标签", "download": "下载预览图",
              "inference": "模型推理", "translate": "翻译标签", "create_tags": "创建标签",
              "assign_tags": "关联标签", "readback": "回读确认", "marker": "写入完成标记",
              "import_model": "加载模型依赖", "load_model": "准备模型（可能检查或下载权重）",
              "load_labels": "准备模型词表", "catalog": "校验中文词典", "checkpoint": "保存任务进度"}


class ProgressState:
    def __init__(self, settings):
        self.settings = settings
        self.logger = logging.getLogger("progress")
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.reset()

    def reset(self):
        now = time.monotonic()
        with self.lock:
            self.data = {"run_id": None, "task_status": "idle", "phase": "idle", "account": None,
                         "scan_pages": 0, "scan_records": 0, "detail_requests": 0, "candidates": 0,
                         "scan_skips": {}, "total": None, "completed": 0, "remaining": None,
                         "session_completed": 0, "current_asset_id": None, "operation": None,
                         "last_progress_at": None, "resumed": False, "next_run_at": None,
                         "processed": 0, "failed": 0, "skipped": 0}
            self._session_started = self._phase_started = now
            self._operation_started = None
            self._processing_started = None
            self._finished = None
            self._last_slow_warning = None

    def update(self, *, advance=False, **values):
        with self.lock:
            self.data.update(values)
            if advance:
                self.data["last_progress_at"] = utc_now()
            if self.data["total"] is not None:
                self.data["remaining"] = max(0, self.data["total"] - self.data["completed"])

    def increment(self, key, amount=1):
        with self.lock:
            self.data[key] += amount
            self.data["last_progress_at"] = utc_now()

    def skip(self, reason):
        with self.lock:
            skips = self.data["scan_skips"]
            skips[reason] = skips.get(reason, 0) + 1
            self.data["last_progress_at"] = utc_now()

    def phase(self, phase, message, *, status=None):
        with self.lock:
            self.data["phase"] = phase
            if status:
                self.data["task_status"] = status
            self._phase_started = time.monotonic()
            if phase == "processing":
                self._processing_started = self._phase_started
        self.log(message)

    @contextmanager
    def operation(self, operation):
        with self.lock:
            previous = (self.data["operation"], self._operation_started, self._last_slow_warning)
            self.data["operation"] = operation
            self._operation_started = time.monotonic()
            self._last_slow_warning = None
        try:
            yield
        finally:
            with self.lock:
                self.data["operation"], self._operation_started, self._last_slow_warning = previous

    def finish(self):
        with self.lock:
            self._finished = time.monotonic()

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            data = copy.deepcopy(self.data)
            execution_end = self._finished if self._finished is not None else now
            data.update(phase_elapsed_seconds=round(now - self._phase_started, 1),
                        operation_elapsed_seconds=round(now - self._operation_started, 1) if self._operation_started else 0,
                        session_elapsed_seconds=round(execution_end - self._session_started, 1),
                        assets_per_second=(round(data["session_completed"] / max(.001, execution_end - self._processing_started), 2)
                                           if self._processing_started else None))
            return data

    def log(self, message, level=logging.INFO):
        data = self.snapshot()
        context = f"｜任务：{data['run_id'][:8]}" if data["run_id"] else ""
        if data["account"]:
            context += f"｜账号：{data['account']}"
        self.logger.log(level, "[%s] %s%s", PHASES[data["phase"]], message, context)

    def report(self):
        data = self.snapshot()
        if data["phase"] == "scanning":
            message = (f"已读取 {data['scan_pages']} 页｜已读取记录 {data['scan_records']} 条"
                       f"｜已选候选 {data['candidates']} 张｜补取详情 {data['detail_requests']} 次"
                       f"｜本地跳过 {data['scan_skips']}")
        elif data["phase"] == "processing":
            total = data["total"]
            percent = f"（{data['completed'] / total:.1%}）" if total else ""
            message = (f"已完成 {data['completed']}/{total}{percent}｜成功 {data['processed']}"
                       f"｜失败 {data['failed']}｜跳过 {data['skipped']}"
                       f"｜本次完成 {data['session_completed']} 张｜本次平均 {data['assets_per_second']} 张/秒")
        else:
            message = f"阶段耗时 {data['phase_elapsed_seconds']:.0f} 秒"
        if data["operation"]:
            message += (f"｜当前操作：{OPERATIONS.get(data['operation'], data['operation'])}"
                        f"｜操作耗时 {data['operation_elapsed_seconds']:.0f} 秒")
        if data["current_asset_id"]:
            message += f"｜图片 ID：{data['current_asset_id']}"
        self.log(message)
        warn = False
        with self.lock:
            now = time.monotonic()
            if (self._operation_started is not None
                    and now - self._operation_started >= self.settings.log_slow_operation_seconds
                    and (self._last_slow_warning is None or now - self._last_slow_warning >= 60)):
                self._last_slow_warning = now
                warn = True
        if warn:
            self.log("仍在等待｜" + message, logging.WARNING)

    def _report_loop(self):
        while not self._stop.wait(self.settings.log_progress_interval_seconds):
            self.report()

    @contextmanager
    def reporting(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._report_loop, name="tagger-progress", daemon=True)
        self._thread.start()
        try:
            yield
        finally:
            self._stop.set()
            self._thread.join()
            self._thread = None
