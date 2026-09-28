"""Single-writer guard and append-only tag-assignment journal."""
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone


def account_scope(settings, account):
    return hashlib.sha256((settings.immich_base_url + "\0" + account["api_key"]).encode()).hexdigest()[:24]


@contextmanager
def writer_lock(directory, dry_run=False):
    if dry_run:
        yield
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "writer.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another tagger is using this state directory") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def record_assignment(directory, scope, asset_id, tags):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "assignments.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "account": scope,
                                 "asset_id": asset_id, "added": tags}, ensure_ascii=False) + "\n")


def record_cleanup(directory, scope, asset_id, *, operation_id, status, tag, translated, run_id):
    """Persist intent before DELETE, and confirmation after readback, using the same ID."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "cleanup.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "account": scope,
                                 "asset_id": asset_id, "operation_id": operation_id, "status": status,
                                 "tag_id": tag.id, "path": tag.path, "translated": translated,
                                 "run_id": run_id}, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def recorded_assignments(directory):
    """Return recorded raw tag paths grouped by account scope and asset ID."""
    path = directory / "assignments.jsonl"
    result = {}
    if not path.exists():
        raise ValueError("recorded 清理需要 state/assignments.jsonl；请恢复历史记录或显式选择 --cleanup-scope catalog")
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if (not isinstance(item, dict)
                        or not all(isinstance(item.get(k), str) and item[k] for k in ("account", "asset_id"))
                        or not isinstance(item.get("added"), list)
                        or not all(isinstance(t, str) and t for t in item["added"])):
                    raise ValueError()
            except ValueError:
                raise ValueError(f"assignments.jsonl 第 {number} 行损坏，清理已取消；请先修复记录") from None
            result.setdefault(item["account"], {}).setdefault(item["asset_id"], set()).update(item["added"])
    return result
