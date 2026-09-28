"""Durable run queue. Callers hold writer_lock for every writable connection."""
import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import RunResult


SCHEMA_VERSION = 1


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class TaskStore:
    def __init__(self, directory, *, memory=False, readonly=False):
        self.path = Path(directory) / "progress.sqlite3"
        self.readonly = readonly
        if memory:
            target, uri = ":memory:", False
        elif readonly:
            target, uri = self.path.resolve().as_uri() + "?mode=ro", True
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target, uri = str(self.path), False
        self.db = sqlite3.connect(target, uri=uri, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if readonly or version != 0 or tables:
                    raise RuntimeError("进度数据库版本不兼容，请恢复兼容版本；不会自动清空状态")
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL, scan_complete INTEGER NOT NULL DEFAULT 0,
                        generation INTEGER NOT NULL DEFAULT 0, total INTEGER,
                        scan_skipped INTEGER NOT NULL DEFAULT 0,
                        result TEXT NOT NULL, scan TEXT NOT NULL DEFAULT '{}', error TEXT,
                        revision TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        last_progress_at TEXT
                    );
                    CREATE TABLE items (
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        account TEXT NOT NULL, asset_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                        generation INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                        reason TEXT, error TEXT,
                        PRIMARY KEY (run_id, account, asset_id)
                    );
                    CREATE INDEX pending_items ON items(run_id, status, ordinal);
                    CREATE TABLE failures (
                        scope TEXT NOT NULL, asset_id TEXT NOT NULL, attempts INTEGER NOT NULL,
                        last_failed TEXT NOT NULL, permanently_failed INTEGER NOT NULL,
                        PRIMARY KEY(scope, asset_id)
                    );
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            self.db.execute("PRAGMA foreign_keys=ON")
            if not readonly:
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=FULL")
                if not memory:
                    self._import_legacy()
        except BaseException:
            self.db.close()
            raise

    def _import_legacy(self):
        if self.db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
            return
        # Import and the marker commit together. Old files remain untouched as backups.
        with self.db:
            for path in sorted(self.path.parent.glob("failures-*.json")):
                scope = path.stem.removeprefix("failures-")
                failures = json.loads(path.read_text(encoding="utf-8"))["failures"]
                for asset_id, item in failures.items():
                    attempts = int(item["attempts"])
                    if attempts < 0:
                        raise ValueError("旧失败记录包含无效次数，迁移已取消")
                    self.db.execute("INSERT INTO failures VALUES (?, ?, ?, ?, ?)",
                                    (scope, asset_id, attempts, item["last_failed"],
                                     int(item["permanently_failed"])))
            self.db.execute("INSERT INTO metadata VALUES ('legacy_imported', '1')")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get_run(self, run_id):
        return dict(self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def find_pending(self, kind, signature):
        row = self.db.execute("SELECT * FROM runs WHERE kind=? AND fingerprint=? "
                              "AND status NOT IN ('completed','superseded') ORDER BY created_at DESC LIMIT 1",
                              (kind, signature)).fetchone()
        return dict(row) if row else None

    def begin(self, kind, signature, revision):
        with self.db:
            incompatible = self.db.execute("SELECT COUNT(*) FROM runs WHERE kind=? AND fingerprint<>? "
                                           "AND status NOT IN ('completed','superseded')", (kind, signature)).fetchone()[0]
            if incompatible:
                logging.getLogger("state").warning("[恢复] 当前配置与 %s 个未完成任务不兼容，旧任务已替代，将建立新任务", incompatible)
            self.db.execute("UPDATE runs SET status='superseded', updated_at=? WHERE kind=? "
                            "AND fingerprint<>? AND status NOT IN ('completed','superseded')",
                            (utc_now(), kind, signature))
            self.db.execute("DELETE FROM items WHERE run_id IN (SELECT id FROM runs WHERE status='superseded')")
            run = self.find_pending(kind, signature)
            if run:
                self.db.execute("UPDATE items SET status='pending' WHERE run_id=? AND status='processing'", (run["id"],))
                self.db.execute("UPDATE runs SET error=NULL, updated_at=? WHERE id=?", (utc_now(), run["id"]))
                return self.get_run(run["id"]), True
            run_id, now = uuid.uuid4().hex, utc_now()
            self.db.execute("INSERT INTO runs (id,kind,fingerprint,status,result,revision,created_at,updated_at) "
                            "VALUES (?,?,?,'scanning',?,?,?,?)",
                            (run_id, kind, signature, RunResult().model_dump_json(), revision, now, now))
            self._prune()
            return self.get_run(run_id), False

    def start_scan(self, run_id):
        with self.db:
            self.db.execute("UPDATE runs SET generation=generation+1, status='scanning', total=NULL, "
                            "scan_skipped=0, result=?, scan='{}', updated_at=? WHERE id=?",
                            (RunResult().model_dump_json(), utc_now(), run_id))
        return self.get_run(run_id)["generation"]

    def save_page(self, run_id, generation, candidates, scan, scan_skipped):
        with self.db:
            self.db.executemany("INSERT INTO items(run_id,account,asset_id,ordinal,generation) VALUES (?,?,?,?,?) "
                                "ON CONFLICT(run_id,account,asset_id) DO UPDATE SET "
                                "generation=excluded.generation,ordinal=excluded.ordinal,status='pending',reason=NULL,error=NULL",
                                [(run_id, account, asset, ordinal, generation) for account, asset, ordinal in candidates])
            self.db.execute("UPDATE runs SET scan=?,scan_skipped=?,result=?,updated_at=?,last_progress_at=? WHERE id=?",
                            (json.dumps(scan), scan_skipped, RunResult(skipped=scan_skipped).model_dump_json(),
                             utc_now(), utc_now(), run_id))

    def finish_scan(self, run_id, generation):
        with self.db:
            self.db.execute("DELETE FROM items WHERE run_id=? AND generation<>?", (run_id, generation))
            self.db.execute("UPDATE runs SET scan_complete=1,status='ready', "
                            "total=(SELECT COUNT(*) FROM items WHERE run_id=?),updated_at=? WHERE id=?",
                            (run_id, utc_now(), run_id))
        return self.get_run(run_id)

    def pending_items(self, run_id):
        # Short read statements, not a cursor held across network calls or writes.
        ordinal = -1
        while True:
            rows = self.db.execute("SELECT * FROM items WHERE run_id=? AND status='pending' AND ordinal>? "
                                   "ORDER BY ordinal LIMIT 100", (run_id, ordinal)).fetchall()
            if not rows:
                return
            for row in rows:
                ordinal = row["ordinal"]
                yield dict(row)

    def mark_processing(self, run_id, account, asset_id):
        with self.db:
            self.db.execute("UPDATE runs SET status='processing',updated_at=? WHERE id=?", (utc_now(), run_id))
            self.db.execute("UPDATE items SET status='processing' WHERE run_id=? AND account=? AND asset_id=?",
                            (run_id, account, asset_id))

    def finish_item(self, run_id, account, item, failure_scope, failure_timeout):
        with self.db:
            previous = self.db.execute("SELECT status FROM items WHERE run_id=? AND account=? AND asset_id=?",
                                       (run_id, account, item.asset_id)).fetchone()
            if previous is None or previous[0] not in ("pending", "processing"):
                raise RuntimeError("图片结果已经提交或不在当前队列中")
            result = RunResult.model_validate_json(self.get_run(run_id)["result"])
            result.attempted += 1
            setattr(result, item.status, getattr(result, item.status) + 1)
            self.db.execute("UPDATE items SET status=?,reason=?,error=? WHERE run_id=? AND account=? AND asset_id=?",
                            (item.status, item.reason, item.error, run_id, account, item.asset_id))
            if item.status == "failed":
                self._record_failure(failure_scope, item.asset_id, failure_timeout)
            elif item.status in ("processed", "skipped"):
                self.db.execute("DELETE FROM failures WHERE scope=? AND asset_id=?", (failure_scope, item.asset_id))
            self.db.execute("UPDATE runs SET result=?,updated_at=?,last_progress_at=? WHERE id=?",
                            (result.model_dump_json(), utc_now(), utc_now(), run_id))
        return result

    def pause(self, run_id, error=None):
        with self.db:
            self.db.execute("UPDATE runs SET status='paused',error=?,updated_at=? WHERE id=?", (error, utc_now(), run_id))

    def complete(self, run_id, error=None):
        with self.db:
            if self.db.execute("SELECT 1 FROM items WHERE run_id=? AND status IN ('pending','processing') LIMIT 1",
                               (run_id,)).fetchone():
                raise RuntimeError("任务仍有待处理图片，不能标记为完成")
            self.db.execute("UPDATE runs SET status='completed',error=?,updated_at=? WHERE id=?", (error, utc_now(), run_id))
            self.db.execute("DELETE FROM items WHERE run_id=?", (run_id,))
            self._prune()

    def _prune(self):
        self.db.execute("DELETE FROM runs WHERE id IN (SELECT id FROM runs WHERE status IN ('completed','superseded') "
                        "ORDER BY updated_at DESC, rowid DESC LIMIT -1 OFFSET 100)")

    def failures(self, scope):
        return {row["asset_id"]: {"attempts": row["attempts"], "last_failed": row["last_failed"],
                                  "permanently_failed": bool(row["permanently_failed"])}
                for row in self.db.execute("SELECT * FROM failures WHERE scope=?", (scope,))}

    def _record_failure(self, scope, asset_id, timeout):
        self.db.execute("INSERT INTO failures VALUES (?,?,1,?,?) ON CONFLICT(scope,asset_id) DO UPDATE SET "
                        "attempts=attempts+1,last_failed=excluded.last_failed,permanently_failed=(attempts+1>=?)",
                        (scope, asset_id, utc_now(), int(timeout <= 1), max(1, timeout)))

    def record_failure(self, scope, asset_id, timeout):
        with self.db:
            self._record_failure(scope, asset_id, timeout)

    def reset_failures(self, scope, ids=None):
        with self.db:
            if ids is None:
                self.db.execute("DELETE FROM failures WHERE scope=?", (scope,))
            else:
                self.db.executemany("DELETE FROM failures WHERE scope=? AND asset_id=?", [(scope, i) for i in ids])

    @classmethod
    def snapshot(cls, directory):
        empty = {"source": "persisted", "live": False, "running": None, "last_error": None,
                 "last_run": RunResult().model_dump(), "translation_revision": None, "progress": None}
        if not (Path(directory) / "progress.sqlite3").exists():
            return {**empty, "message": "尚无持久化任务记录"}
        with cls(directory, readonly=True) as store:
            row = store.db.execute("SELECT * FROM runs ORDER BY updated_at DESC, rowid DESC LIMIT 1").fetchone()
            if row is None:
                return {**empty, "message": "尚无持久化任务记录"}
            result = json.loads(row["result"])
            phase = row["status"]
            if phase == "ready":
                phase = "preparing_model" if row["kind"] == "inference" else "processing"
            elif phase == "superseded":
                phase = "idle"
            return {**empty, "last_error": row["error"], "last_run": result, "translation_revision": row["revision"],
                    "progress": {"run_id": row["id"], "task_status": row["status"],
                                 "phase": phase,
                                 "total": row["total"], "scan_skipped": row["scan_skipped"],
                                 "completed": result["attempted"],
                                 "remaining": None if row["total"] is None else row["total"] - result["attempted"],
                                 "scan_complete": bool(row["scan_complete"]), "scan": json.loads(row["scan"]),
                                 "updated_at": row["updated_at"], "last_progress_at": row["last_progress_at"]}}
