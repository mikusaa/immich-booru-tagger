"""Compatibility facade for SQLite failures, with read-only legacy fallback."""
import json
from pathlib import Path

from .config import get_settings
from .state import writer_lock
from .task_store import TaskStore


class FailureTracker:
    def __init__(self, library_name="default", failure_file=None, settings=None):
        self.settings = settings or get_settings()
        self.library_name = library_name
        self.failure_file = Path(failure_file or self.settings.state_dir / f"failures-{library_name}.json")

    @property
    def failures(self):
        if (self.settings.state_dir / "progress.sqlite3").exists():
            with TaskStore(self.settings.state_dir, readonly=True) as store:
                return store.failures(self.library_name)
        if self.failure_file.exists():
            return json.loads(self.failure_file.read_text(encoding="utf-8"))["failures"]
        return {}

    def record_failure(self, asset_id):
        with writer_lock(self.settings.state_dir), TaskStore(self.settings.state_dir) as store:
            store.record_failure(self.library_name, asset_id, self.settings.failure_timeout)
            return not store.failures(self.library_name)[asset_id]["permanently_failed"]

    def is_permanently_failed(self, asset_id):
        return self.failures.get(asset_id, {}).get("permanently_failed", False)

    def reset_failures(self, asset_ids=None):
        with writer_lock(self.settings.state_dir), TaskStore(self.settings.state_dir) as store:
            store.reset_failures(self.library_name, asset_ids)

    def get_failed_assets(self):
        return self.failures

    def get_permanently_failed_assets(self):
        return {key: value for key, value in self.failures.items() if value["permanently_failed"]}

    def get_failure_summary(self):
        failures = self.failures
        permanent = sum(item["permanently_failed"] for item in failures.values())
        return {"total_failed_assets": len(failures), "permanently_failed": permanent,
                "retry_candidates": len(failures) - permanent, "failure_timeout": self.settings.failure_timeout}
