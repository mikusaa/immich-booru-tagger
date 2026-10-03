import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import shutil
import subprocess
import sys
import tarfile

import pytest

from app.main import main, parse_arguments
from tools.macos_launcher import LABEL, MacLauncher


@pytest.fixture
def launcher(settings, tmp_path, monkeypatch):
    root = tmp_path / "Mac project with spaces"
    root.mkdir()
    launcher = MacLauncher(root, agents_dir=tmp_path / "LaunchAgents")
    launcher.save({"IMMICH_BASE_URL": "http://immich.test", "IMMICH_API_KEY": "secret-key",
                   "STATE_DIR": str(root / "state"), "MODEL_CACHE_DIR": str(root / "models")})
    monkeypatch.setattr(launcher, "loaded", lambda: False)
    return launcher


def test_native_config_preserves_container_file_and_special_key(settings, tmp_path):
    original = "IMMICH_BASE_URL=http://test\nIMMICH_API_KEY=key\nSTATE_DIR=/app/state\nMODEL_CACHE_DIR=/app/models\nGENERAL_THRESHOLD=0.4\n"
    (tmp_path / ".env").write_text(original)
    launcher = MacLauncher(tmp_path)
    values = launcher.values()
    values["IMMICH_API_KEY"] = "key' with # and \\slashes $dollar"
    launcher.save(values)
    assert (tmp_path / ".env").read_text() == original
    assert launcher.settings().immich_api_key == values["IMMICH_API_KEY"]
    assert launcher.settings().state_dir == tmp_path / "state"
    assert launcher.settings().model_cache_dir == tmp_path / "models"
    assert launcher.settings().effective_general_threshold == .4
    assert launcher.profile.stat().st_mode & 0o777 == 0o600


def test_invalid_config_is_not_saved(launcher):
    before = launcher.profile.read_bytes()
    values = launcher.values()
    values["TAGGING_CPU_THREADS"] = "-1"
    with pytest.raises(ValueError):
        launcher.save(values)
    assert launcher.profile.read_bytes() == before


def test_connection_failure_does_not_replace_profile_or_leak_key(launcher, monkeypatch):
    answers = iter(["http://other", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _: "new-private-key")
    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["dry_run"] is True
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def test_connection(self):
            raise ValueError("invalid new-private-key")
    monkeypatch.setattr("app.immich_client.ImmichClient", Client)
    before = launcher.profile.read_bytes()
    with pytest.raises(ValueError) as error:
        launcher.configure()
    assert "new-private-key" not in str(error.value)
    assert launcher.profile.read_bytes() == before


def test_preview_failure_blocks_first_write(launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "run_cli", lambda *args: calls.append(args) or 1)
    assert not launcher.ensure_preview()
    assert calls == [("--dry-run", "--limit", "20")]
    assert not launcher.preview_record.exists()


def test_preview_fingerprint_survives_device_switch_but_not_scope_change(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "run_cli", lambda *args: 0)
    assert launcher.preview()
    signature = launcher.preview_signature()
    values = launcher.values()
    values.update(TAGGING_DEVICE="cpu", TAGGING_CPU_THREADS="4", CRON_SCHEDULE="15 3 * * *")
    launcher.save(values)
    assert launcher.preview_signature() == signature
    assert launcher.ensure_preview()
    values["IMMICH_INCLUDE_ALBUM_IDS"] = "00000000-0000-0000-0000-000000000001"
    launcher.save(values)
    assert launcher.preview_signature() != signature
    assert "secret-key" not in launcher.preview_record.read_text()


def test_launchd_install_and_stop_use_absolute_paths_without_credentials(launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "ensure_preview", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "03:15")
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    launcher.enable_schedule()
    record = plistlib.loads(launcher.agent.read_bytes())
    assert record["ProgramArguments"] == launcher.command("--mode", "scheduler")
    assert record["WorkingDirectory"] == str(launcher.root)
    assert record["RunAtLoad"] is True and record["KeepAlive"] is False
    assert "secret-key" not in launcher.agent.read_text()
    assert launcher.settings().cron_schedule == "15 3 * * *"
    assert calls == [["launchctl", "bootstrap", launcher.domain, str(launcher.agent)]]
    state = launcher.root / "state"
    state.mkdir()
    (state / "keep").write_text("progress")
    monkeypatch.setattr(launcher, "loaded", lambda: True)
    launcher.disable_schedule()
    assert calls[-1] == ["launchctl", "bootout", f"{launcher.domain}/{LABEL}"]
    assert not launcher.agent.exists()
    assert (state / "keep").read_text() == "progress"
    assert launcher.profile.exists()


def test_other_installation_is_not_overwritten_or_stopped(launcher):
    launcher.agent.parent.mkdir()
    launcher.agent.write_bytes(plistlib.dumps({"WorkingDirectory": "/another/project"}))
    with pytest.raises(ValueError, match="另一个"):
        launcher.enable_schedule()
    with pytest.raises(ValueError, match="另一个"):
        launcher.disable_schedule()
    assert launcher.agent.exists()


def test_running_scheduler_blocks_manual_writes_and_configuration(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "loaded", lambda: True)
    with pytest.raises(ValueError, match="关闭定时"):
        launcher.preview()
    with pytest.raises(ValueError, match="关闭定时"):
        launcher.configure()


def test_cli_custom_env_file_does_not_read_container_config(settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.main.setup_logging", lambda *args: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ENGLISH_TAGS_ENABLED=false\n")
    native = tmp_path / "native.env"
    native.write_text(f"IMMICH_BASE_URL=http://test\nIMMICH_API_KEY=native-key\nSTATE_DIR={tmp_path / 'state'}\n")
    assert parse_arguments(["--env-file", str(native)]).env_file == native
    assert main(["--env-file", str(native), "--progress-status"]) == 0
    assert "native-key" not in capsys.readouterr().out
    assert main(["--env-file", str(tmp_path / "missing"), "--progress-status"]) == 1


def test_launcher_import_and_status_do_not_load_ml(settings, tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", "import sys; import tools.macos_launcher; "
                             "assert 'torch' not in sys.modules; assert 'wdtagger' not in sys.modules"],
                            cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_album_selection_uses_names_and_ids_without_writes(launcher, monkeypatch, capsys):
    import httpx
    from app.immich_client import ImmichClient
    albums = [{"id": f"00000000-0000-0000-0000-{i:012d}", "albumName": f"Album {i}"} for i in (1, 2)]
    calls = []
    def response(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=albums)
    def client(settings, **kwargs):
        return ImmichClient(settings, **kwargs, transport=httpx.MockTransport(response))
    monkeypatch.setattr("app.immich_client.ImmichClient", client)
    monkeypatch.setattr("builtins.input", lambda _: "2,1,2")
    assert launcher.select_albums(launcher.values()) == albums[1]["id"] + "," + albums[0]["id"]
    assert calls == [("GET", "/api/albums")]
    assert "Album 2" in capsys.readouterr().out


def test_foreground_interrupt_waits_for_child_and_keeps_logs(launcher, monkeypatch):
    class Child:
        stdout = io.StringIO("checkpoint saved\n")
        calls = 0
        signals = []
        def wait(self):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt()
            return 0
        def send_signal(self, sig):
            self.signals.append(sig)
    child = Child()
    commands = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: commands.append(command) or child)
    assert launcher.run_cli("--mode", "continuous") == 130
    assert child.signals == [signal.SIGTERM]
    assert child.calls == 2
    assert "checkpoint saved" in (launcher.logs / "macos-run.log").read_text()
    assert commands == [launcher.command("--mode", "continuous")]


@pytest.mark.parametrize("installed_uv", [True, False])
def test_bootstrap_download_or_existing_uv_is_reused(tmp_path, installed_uv):
    root = tmp_path / "Mac project with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1]
    shutil.copy(repo / "scripts/bootstrap_macos.sh", scripts)
    (root / "requirements.txt").write_text("-r requirements-core.txt\n")
    (root / "requirements-core.txt").write_text("httpx==0.28.1\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    def executable(path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/bash\n" + source)
        path.chmod(0o755)
    executable(fake_bin / "uname", 'if [[ "$1" == -s ]]; then echo Darwin; else echo arm64; fi\n')
    executable(fake_bin / "launchctl", "exit 1\n")
    hash_tool = tmp_path / "hash_tool.py"
    hash_tool.write_text("import hashlib, sys\nfrom pathlib import Path\n"
                         "for name in sys.argv[3:]:\n"
                         "    print(hashlib.sha256(Path(name).read_bytes()).hexdigest(), name)\n")
    executable(fake_bin / "shasum", 'exec "$REAL_PYTHON" "$HASH_TOOL" "$@"\n')
    executable(root / ".macos/venv/bin/python", 'if [[ "$1" == -m ]]; then echo menu-ready; fi\n')
    uv_source = ('#!/bin/bash\nif [[ "$1" == --version ]]; then echo "uv 0.12.19"; exit 0; fi\n'
                 'printf "%s\\n" "$*" >> "$BOOTSTRAP_CALLS"\n'
                 'if [[ "$1" == pip && -f "$BOOTSTRAP_FAIL" ]]; then rm "$BOOTSTRAP_FAIL"; exit 1; fi\n')
    if installed_uv:
        executable(fake_bin / "uv", uv_source)
    else:
        archive = tmp_path / "uv.tar.gz"
        with tarfile.open(archive, "w:gz") as target:
            data = uv_source.encode()
            member = tarfile.TarInfo("uv-aarch64-apple-darwin/uv")
            member.size, member.mode = len(data), 0o755
            target.addfile(member, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = tmp_path / "uv.sha256"
        checksum.write_text(f"{digest}  uv-aarch64-apple-darwin.tar.gz\n")
        executable(fake_bin / "curl", 'for arg in "$@"; do if [[ "$arg" == *.sha256 ]]; then checksum=1; fi; done\n'
                   'while [[ "$1" != -o ]]; do shift; done\n'
                   'if [[ "$checksum" == 1 ]]; then cp "$FAKE_CHECKSUM" "$2"; else cp "$FAKE_ARCHIVE" "$2"; fi\n')
    calls = tmp_path / "calls"
    env = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "BOOTSTRAP_CALLS": str(calls),
           "BOOTSTRAP_FAIL": str(tmp_path / "fail-once"),
           "REAL_PYTHON": sys.executable, "HASH_TOOL": str(hash_tool),
           "FAKE_CHECKSUM": str(tmp_path / "uv.sha256"), "FAKE_ARCHIVE": str(tmp_path / "uv.tar.gz")}
    first = subprocess.run(["/bin/bash", str(scripts / "bootstrap_macos.sh")], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "menu-ready" in first.stdout
    installed = calls.read_text()
    assert "python install --no-bin 3.11" in installed and "pip install --python" in installed
    second = subprocess.run(["/bin/bash", str(scripts / "bootstrap_macos.sh")], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert calls.read_text() == installed
    assert not (root / ".macos/setup.lock").exists()
    marker = (root / ".macos/requirements.sha256").read_bytes()
    (root / "requirements-core.txt").write_text("httpx==0.28.1\n# updated\n")
    (tmp_path / "fail-once").touch()
    failed = subprocess.run(["/bin/bash", str(scripts / "bootstrap_macos.sh")], env=env, capture_output=True, text=True)
    assert failed.returncode == 1
    assert (root / ".macos/requirements.sha256").read_bytes() == marker
    assert not (root / ".macos/setup.lock").exists()
    retry = subprocess.run(["/bin/bash", str(scripts / "bootstrap_macos.sh")], env=env, capture_output=True, text=True)
    assert retry.returncode == 0, retry.stderr
    assert (root / ".macos/requirements.sha256").read_bytes() != marker
