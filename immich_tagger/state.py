"""Single-writer guard and append-only tag-assignment journal."""
import fcntl
import hashlib
import json
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
