"""Durable tag baseline, independent of queue restoration and task checkpoints."""
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

from .cleanup_queues import QueueMaintenanceError
from .state import account_scope

STATE_FILE = "cleanup-pending.json"


class CleanupSafetyError(QueueMaintenanceError):
    """An unresolved asset must never become an ordinary empty-asset skip."""


class CleanupBaseline:
    # The caller holds writer_lock. Only one asset may be in flight at a time.
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.state_dir / STATE_FILE
        self.server = hashlib.sha256(settings.immich_base_url.encode()).hexdigest()

    def _sync_directory(self):
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def begin(self, client, asset, pairs, *, run_id, maintenance):
        if self.path.exists():
            raise CleanupSafetyError("存在未复核的清理标签基线，请先续跑清理核验 cleanup-pending.json")
        planned = {tag.id: translated for tag, translated in pairs}
        state = {
            "version": 1, "operation_id": uuid.uuid4().hex,
            "at": datetime.now(timezone.utc).isoformat(), "server": self.server,
            "account": account_scope(self.settings, client.account), "asset_id": asset.id,
            "run_id": run_id, "maintenance": maintenance,
            "tags": {tag.id: tag.path for tag in asset.tags},
            "planned": planned,
            "protected": sorted(tag.id for tag in asset.tags if tag.id not in planned),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._sync_directory()
        except OSError as error:
            raise CleanupSafetyError("保存清理标签基线失败，未开始删除；请检查 cleanup-pending.json") from error
        return state

    def load(self, clients):
        if not self.path.exists():
            return None
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1
                    or state.get("server") != self.server
                    or state.get("account") not in {account_scope(self.settings, c.account) for c in clients}
                    or not all(isinstance(state.get(k), str) and state[k] for k in ("asset_id", "operation_id", "at"))
                    or not (state.get("run_id") is None or isinstance(state["run_id"], str))
                    or type(state.get("maintenance")) is not bool):
                raise ValueError("Invalid baseline identity")
            tags, planned, protected = state["tags"], state["planned"], state["protected"]
            for mapping in (tags, planned):
                if (not isinstance(mapping, dict) or not mapping
                        or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in mapping.items())):
                    raise ValueError("Invalid baseline tag paths")
            if (not isinstance(protected, list) or not all(isinstance(t, str) for t in protected)
                    or len(set(protected)) != len(protected) or not set(planned) <= tags.keys()
                    or set(protected) != tags.keys() - planned.keys()
                    or not set(planned.values()) <= {tags[t] for t in protected}):
                raise ValueError("Invalid baseline partition")
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise CleanupSafetyError(
                "清理标签基线损坏或服务/账号不匹配；请核对 cleanup-pending.json，不能跳过复核"
            ) from error
        return state

    def verify(self, state, asset):
        if asset.id != state["asset_id"] or asset.tags is None:
            raise CleanupSafetyError("无法读取待复核图片的完整标签；保留 cleanup-pending.json")
        present = {tag.id for tag in asset.tags}
        missing = set(state["protected"]) - present
        if missing:
            details = "、".join(f"{state['tags'][tid]}（{tid}）" for tid in sorted(missing))
            raise CleanupSafetyError(
                f"后台复核发现保留标签缺失：{asset.id}｜缺失：{details}｜"
                "停止清理，基线保存在 cleanup-pending.json；修复存储/XMP 问题并恢复缺失保留标签后再续跑"
            )
        return present

    def resolve(self, state, present, *, outcome):
        # Archive the full baseline before removing the latch. A crash between
        # these operations may duplicate history but cannot lose pending intent.
        record = {**state, "verified_at": datetime.now(timezone.utc).isoformat(),
                  "outcome": outcome, "remaining_english": sorted(state["planned"].keys() & present)}
        try:
            with (self.path.parent / "cleanup-baselines.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_directory()
            self.path.unlink()
            self._sync_directory()
        except OSError as error:
            raise CleanupSafetyError("保存清理复核结果失败；请保留状态目录后续跑") from error
