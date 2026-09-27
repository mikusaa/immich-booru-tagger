"""Atomic per-account failure records, shared by all execution modes."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings


class FailureTracker:
    def __init__(self, library_name="default", failure_file=None, settings=None):
        self.settings = settings or get_settings()
        self.library_name = library_name
        self.failure_file = Path(failure_file or self.settings.state_dir / f"failures-{library_name}.json")
        self.failures = {}
        if self.failure_file.exists():
            self.failures = json.loads(self.failure_file.read_text(encoding="utf-8"))["failures"]

    def save_failures(self):
        self.failure_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.failure_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"failures": self.failures}, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.failure_file)

    def record_failure(self, asset_id):
        attempts = self.failures.get(asset_id, {}).get("attempts", 0) + 1
        permanent = attempts >= max(1, self.settings.failure_timeout)
        self.failures[asset_id] = {
            "attempts": attempts, "last_failed": datetime.now(timezone.utc).isoformat(),
            "permanently_failed": permanent,
        }
        self.save_failures()
        return not permanent

    def is_permanently_failed(self, asset_id):
        return self.failures.get(asset_id, {}).get("permanently_failed", False)

    def reset_failures(self, asset_ids=None):
        if asset_ids is None:
            self.failures.clear()
        else:
            for asset_id in asset_ids:
                self.failures.pop(asset_id, None)
        self.save_failures()

    def get_failed_assets(self):
        return dict(self.failures)

    def get_permanently_failed_assets(self):
        return [asset_id for asset_id in self.failures if self.is_permanently_failed(asset_id)]

    def get_failure_summary(self):
        permanent = self.get_permanently_failed_assets()
        return {
            "total_failed_assets": len(self.failures),
            "permanently_failed": len(permanent),
            "retry_candidates": len(self.failures) - len(permanent),
            "failure_timeout": self.settings.failure_timeout,
        }
