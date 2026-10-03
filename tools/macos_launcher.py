"""Interactive native Mac launcher; inference remains in the existing CLI."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import threading

from dotenv import dotenv_values, set_key

from app.config import Settings
from app.logging import safe_error

LABEL = "io.github.mikusaa.immich-booru-tagger"


class MacLauncher:
    def __init__(self, root, *, agents_dir=None):
        self.root = Path(root).resolve()
        self.profile = self.root / ".env.macos"
        self.runtime = self.root / ".macos"
        self.logs = self.root / "logs"
        self.python = self.runtime / "venv/bin/python"
        self.preview_record = self.runtime / "preview.json"
        self.agent = (Path(agents_dir) if agents_dir else Path.home() / "Library/LaunchAgents") / f"{LABEL}.plist"
        self.domain = f"gui/{os.getuid()}"

    def values(self):
        source = self.profile if self.profile.exists() else self.root / ".env"
        values = {k.upper(): v for k, v in dotenv_values(source).items()
                  if k.lower() in Settings.model_fields and v is not None}
        # Container-only paths are adapted in the native copy, never in .env.
        for key, default in (("STATE_DIR", "state"), ("MODEL_CACHE_DIR", "models")):
            path = values.get(key, default)
            values[key] = path[5:] if path.startswith("/app/") else path
        for key in ("TRANSLATION_FILE", "TRANSLATION_OVERRIDES"):
            if values.get(key):
                path = values[key]
                if path.startswith("/app/"):
                    path = path[5:]
                values[key] = path
        values.setdefault("TAGGING_DEVICE", "auto")
        return values

    def settings(self, values=None):
        options = {k.lower(): v for k, v in (values if values is not None else self.values()).items()}
        for key in ("state_dir", "model_cache_dir", "translation_file", "translation_overrides"):
            if options.get(key):
                options[key] = (self.root / options[key]).resolve()
        return Settings(_env_file=None, **options)

    def save(self, values):
        self.settings(values)
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".env.macos-", dir=self.root)
        os.close(fd)
        try:
            for key, value in values.items():
                set_key(temporary, key, str(value), quote_mode="always")
            os.replace(temporary, self.profile)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def loaded(self):
        return subprocess.run(["launchctl", "print", f"{self.domain}/{LABEL}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def require_stopped(self):
        if self.loaded():
            raise ValueError("定时服务已开启，请先选“关闭定时运行”，再处理图片或修改配置。")

    def configure(self):
        self.require_stopped()
        values = self.values()
        old_url = values.get("IMMICH_BASE_URL", "")
        if old_url == "http://immich-server:2283":
            old_url = ""
        values["IMMICH_BASE_URL"] = input(f"Immich 地址{f' [{old_url}]' if old_url else ''}：").strip() or old_url
        has_key = values.get("IMMICH_API_KEY") not in (None, "", "replace-with-your-api-key")
        key = getpass.getpass("API Key（输入隐藏" + ("，留空保留原值" if has_key else "") + "）：").strip()
        values["IMMICH_API_KEY"] = key or (values.get("IMMICH_API_KEY", "") if has_key else "")
        values.pop("IMMICH_API_KEYS", None)
        values.pop("IMMICH_LIBRARIES", None)
        print("处理范围：1 当前账号可见的全部图片；2 指定相册；3 指定外部库；回车保留现有范围")
        scope = input("选择范围 [保留]：").strip()
        if not scope and not any(values.get(k) for k in ("IMMICH_INCLUDE_ALBUM_IDS", "IMMICH_INCLUDE_LIBRARY_IDS")):
            scope = "1"
        if scope:
            if scope not in ("1", "2", "3"):
                raise ValueError("请选择 1、2 或 3。")
            values["IMMICH_INCLUDE_ALBUM_IDS"] = ""
            values["IMMICH_INCLUDE_LIBRARY_IDS"] = ""
            if scope != "1":
                key = "IMMICH_INCLUDE_ALBUM_IDS" if scope == "2" else "IMMICH_INCLUDE_LIBRARY_IDS"
                values[key] = self.select_albums(values) if scope == "2" else input("外部库 UUID（逗号分隔）：").strip()
                if not values[key]:
                    raise ValueError("指定范围不能为空。")
        settings = self.settings(values)
        print("正在检查连接与处理范围……")
        from app.immich_client import ImmichClient
        for account in settings.get_library_config():
            with ImmichClient(settings, account, dry_run=True) as client:
                try:
                    client.test_connection()
                    client._album_asset_ids()
                except Exception as error:
                    raise ValueError(safe_error(error, [account["api_key"]])) from None
        if not self.profile.exists():
            for port in range(settings.health_port, min(settings.health_port + 100, 65536)):
                with socket.socket() as listener:
                    try:
                        listener.bind(("0.0.0.0", port))
                    except OSError:
                        continue
                values["HEALTH_PORT"] = str(port)
                break
            else:
                raise ValueError("本地状态端口不可用，请关闭占用端口的进程后重试。")
        self.save(values)
        print("配置已保存，连接成功。")

    def select_albums(self, values):
        from app.immich_client import ImmichClient
        settings = self.settings(values)
        with ImmichClient(settings, dry_run=True) as client:
            try:
                albums = client._make_request("GET", "/api/albums").json()
            except Exception as error:
                raise ValueError(safe_error(error, [settings.immich_api_key])) from None
        if not isinstance(albums, list):
            raise ValueError("无法读取相册列表，请检查 Immich 版本。")
        if not albums:
            raise ValueError("此账号暂无可选相册。")
        for index, album in enumerate(albums, 1):
            print(f"{index} {album.get('albumName', '未命名相册')}")
        choices = input("相册序号（逗号分隔）：").strip().split(",")
        if any(not choice.strip().isdigit() or not 1 <= int(choice) <= len(albums) for choice in choices):
            raise ValueError("请输入列表中的相册序号。")
        return ",".join(dict.fromkeys(albums[int(choice) - 1]["id"] for choice in choices))

    def command(self, *args):
        return [str(self.python), "-m", "app.main", "--env-file", str(self.profile), *args]

    def run_cli(self, *args):
        self.logs.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        with (self.logs / "macos-run.log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen(self.command(*args), cwd=self.root, env=env, start_new_session=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")

            def display():
                for line in child.stdout:
                    log.write(line)
                    log.flush()
                    try:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    except OSError:
                        pass

            reader = threading.Thread(target=display, daemon=True)
            reader.start()
            try:
                code = child.wait()
            except KeyboardInterrupt:
                try:
                    print("\n正在停止，等待当前图片操作完成……")
                except OSError:
                    pass
                child.send_signal(signal.SIGTERM)
                while True:
                    try:
                        child.wait()
                        break
                    except KeyboardInterrupt:
                        continue
                code = 130
            finally:
                reader.join()
                child.stdout.close()
        return code

    def preview_signature(self):
        # Store only a fingerprint, never credentials or the full configuration.
        settings = self.settings().model_dump(mode="json")
        for key in ("tagging_device", "tagging_cpu_threads", "cron_schedule", "enable_scheduler",
                    "run_on_startup", "resume_on_startup", "health_port", "log_level"):
            settings.pop(key, None)
        return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()

    def preview(self):
        self.require_stopped()
        print("预览最多 20 张图片；首次使用会下载默认模型。")
        code = self.run_cli("--dry-run", "--limit", "20")
        if code == 0:
            self.runtime.mkdir(parents=True, exist_ok=True)
            self.preview_record.write_text(json.dumps({"signature": self.preview_signature()}))
            print("预览完成，尚未写入标签。")
        return code == 0

    def ensure_preview(self):
        try:
            if json.loads(self.preview_record.read_text())["signature"] == self.preview_signature():
                return True
        except (OSError, ValueError, KeyError, TypeError):
            pass
        print("此配置尚未完成预览，先检查少量图片。")
        return self.preview() and input("按当前范围开始正式运行？[y/N]：").strip().lower() == "y"

    def plist(self):
        return {"Label": LABEL, "ProgramArguments": self.command("--mode", "scheduler"),
                "WorkingDirectory": str(self.root), "RunAtLoad": True, "KeepAlive": False,
                "StandardOutPath": str(self.logs / "macos-scheduler.log"),
                "StandardErrorPath": str(self.logs / "macos-scheduler.log")}

    def owns_agent(self):
        if self.agent.exists():
            with self.agent.open("rb") as source:
                if plistlib.load(source).get("WorkingDirectory") != str(self.root):
                    raise ValueError("另一个项目目录已安装定时任务，请先用它的启动器关闭。")

    def enable_schedule(self):
        self.owns_agent()
        self.require_stopped()
        if not self.ensure_preview():
            return
        time = input("每天运行时间 [02:00]：").strip() or "02:00"
        parts = time.split(":")
        if (len(parts) != 2 or any(not part.isdigit() for part in parts)
                or not 0 <= int(parts[0]) < 24 or not 0 <= int(parts[1]) < 60):
            raise ValueError("时间格式为 HH:MM，例如 02:00。")
        values = self.values()
        values.update(CRON_SCHEDULE=f"{int(parts[1])} {int(parts[0])} * * *", ENABLE_SCHEDULER="true",
                      RUN_ON_STARTUP="false", RESUME_ON_STARTUP="true")
        self.save(values)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.agent.parent.mkdir(parents=True, exist_ok=True)
        with self.agent.open("wb") as target:
            plistlib.dump(self.plist(), target)
        subprocess.run(["launchctl", "bootstrap", self.domain, str(self.agent)], check=True)
        print(f"已开启登录后定时运行：每天 {time}（{self.settings().timezone}）。")
        print("未完成队列会优先续跑。休眠期间不保证准时执行。")

    def disable_schedule(self):
        self.owns_agent()
        if self.loaded():
            subprocess.run(["launchctl", "bootout", f"{self.domain}/{LABEL}"], check=True)
        self.agent.unlink(missing_ok=True)
        print("定时运行已关闭，模型与处理进度保留。")

    def advanced(self):
        self.require_stopped()
        values = self.values()
        device = input(f"设备 auto/cpu/mps [{values.get('TAGGING_DEVICE', 'auto')}]：").strip()
        if device:
            if device not in ("auto", "cpu", "mps"):
                raise ValueError("请选择 auto、cpu 或 mps。")
            values["TAGGING_DEVICE"] = device
        threads = input("CPU 线程数（回车保留，0 恢复框架默认）：").strip()
        if threads == "0":
            values.pop("TAGGING_CPU_THREADS", None)
        elif threads:
            values["TAGGING_CPU_THREADS"] = threads
        language = input(f"标签语言 bilingual/chinese/english [{values.get('TAG_LANGUAGE_MODE', 'bilingual')}]：").strip()
        if language:
            values["TAG_LANGUAGE_MODE"] = language
        port = input(f"本地状态端口 [{values.get('HEALTH_PORT', '8000')}]：").strip()
        if port:
            values["HEALTH_PORT"] = port
        self.save(values)
        print("设置已保存；切换设备保留当前未完成队列。")

    def status(self):
        print("登录后定时运行：" + ("已加载" if self.loaded() else "已关闭或已退出"))
        self.run_cli("--progress-status")
        for name in ("macos-run.log", "macos-scheduler.log"):
            path = self.logs / name
            if path.exists():
                print(f"\n{path}（最近 20 行）：")
                from collections import deque
                with path.open(encoding="utf-8", errors="replace") as log:
                    print("".join(deque(log, maxlen=20)), end="")

    def report_error(self, error):
        values = self.values()
        secrets = [values.get(key, "") for key in ("IMMICH_API_KEY", "CLEANUP_ADMIN_API_KEY")]
        print("操作未完成：" + safe_error(error, secrets))

    def menu(self):
        while not self.profile.exists():
            print("首次设置")
            try:
                self.configure()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.report_error(error)
        if not self.preview_record.exists() and not self.loaded():
            self.preview()
        while True:
            print("\nImmich Booru Tagger · Mac")
            print("1 开始处理／继续上次任务\n2 预览标签\n3 修改配置\n4 开启登录后定时运行"
                  "\n5 关闭定时运行\n6 查看状态与日志\n7 高级设置\n0 退出")
            choice = input("选择：").strip()
            try:
                if choice == "0":
                    return 0
                if choice == "1":
                    self.require_stopped()
                    if self.ensure_preview():
                        self.run_cli("--mode", "continuous")
                elif choice == "2":
                    self.preview()
                elif choice == "3":
                    self.configure()
                elif choice == "4":
                    self.enable_schedule()
                elif choice == "5":
                    self.disable_schedule()
                elif choice == "6":
                    self.status()
                elif choice == "7":
                    self.advanced()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.report_error(error)


def main():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        print("此启动器用于 Apple Silicon Mac。")
        return 1
    launcher = MacLauncher(Path(__file__).resolve().parents[1])
    def stop(*_):
        raise KeyboardInterrupt()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return launcher.menu()
    except (EOFError, KeyboardInterrupt):
        try:
            print("\n已退出，配置和进度保留。")
        except OSError:
            pass
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Validation errors hide their input; connection errors are redacted at source.
        print("启动未完成：" + safe_error(error))
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
