#!/bin/bash
set -euo pipefail

TASK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASK_RUNTIME="$TASK_ROOT/.macos"
UV_VERSION=0.12.19

if [[ "$(uname -s)" != Darwin ]]; then
    printf '此启动器用于 Apple Silicon Mac。\n' >&2
    exit 1
fi
if [[ "$(uname -m)" != arm64 ]]; then
    if [[ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || true)" == 1 ]]; then
        exec /usr/bin/arch -arm64 /bin/bash "$0" "$@"
    fi
    printf '需要 Apple Silicon Mac（M 系列芯片）。\n' >&2
    exit 1
fi

cd "$TASK_ROOT"
mkdir -p "$TASK_RUNTIME"
if [[ -f "$TASK_RUNTIME/setup.lock/pid" ]]; then
    TASK_INSTALL_PID="$(cat "$TASK_RUNTIME/setup.lock/pid")"
    if [[ "$TASK_INSTALL_PID" =~ ^[0-9]+$ ]] && ! kill -0 "$TASK_INSTALL_PID" 2>/dev/null; then
        rm "$TASK_RUNTIME/setup.lock/pid"
        rmdir "$TASK_RUNTIME/setup.lock"
    fi
fi
if ! mkdir "$TASK_RUNTIME/setup.lock" 2>/dev/null; then
    printf '另一个安装窗口正在运行；请先完成或关闭那个窗口。\n' >&2
    exit 1
fi
printf '%s\n' "$$" > "$TASK_RUNTIME/setup.lock/pid"
TASK_DOWNLOAD=
cleanup() {
    if [[ -n "$TASK_DOWNLOAD" ]]; then rm -rf "$TASK_DOWNLOAD"; fi
    rm "$TASK_RUNTIME/setup.lock/pid"
    rmdir "$TASK_RUNTIME/setup.lock"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

export UV_CACHE_DIR="$TASK_RUNTIME/cache"
export UV_PYTHON_INSTALL_DIR="$TASK_RUNTIME/python"
export UV_PYTHON_PREFERENCE=only-managed
TASK_PYTHON="$TASK_RUNTIME/venv/bin/python"
TASK_REQUIREMENTS="$(shasum -a 256 requirements-core.txt requirements.txt)"
TASK_INSTALLED="$(cat "$TASK_RUNTIME/requirements.sha256" 2>/dev/null || true)"

if [[ "$TASK_REQUIREMENTS" != "$TASK_INSTALLED" ]] || ! "$TASK_PYTHON" -c \
    'import platform, sys; assert platform.machine() == "arm64" and sys.version_info[:2] == (3, 11)' 2>/dev/null; then
    if launchctl print "gui/$(id -u)/io.github.mikusaa.immich-booru-tagger" >/dev/null 2>&1; then
        if [[ -x "$TASK_PYTHON" ]]; then
            printf '环境需要更新，请先在菜单关闭定时运行，再重新打开启动器。\n'
            cleanup
            trap - EXIT
            exec "$TASK_PYTHON" -m tools.macos_launcher "$@"
        fi
        printf '更新环境前请先关闭旧目录的定时任务，停止方法见 macOS 文档。\n' >&2
        exit 1
    fi
    printf '正在准备运行环境，首次下载需要一些时间……\n'
    if command -v uv >/dev/null 2>&1 && [[ "$(uv --version)" == "uv $UV_VERSION"* ]]; then
        TASK_UV="$(command -v uv)"
    else
        TASK_UV="$TASK_RUNTIME/bin/uv"
        if [[ ! -x "$TASK_UV" ]] || [[ "$("$TASK_UV" --version)" != "uv $UV_VERSION"* ]]; then
            TASK_DOWNLOAD="$(mktemp -d "$TASK_RUNTIME/download.XXXXXX")"
            TASK_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz"
            curl --fail --location --retry 3 "$TASK_URL" -o "$TASK_DOWNLOAD/uv.tar.gz"
            curl --fail --location --retry 3 "$TASK_URL.sha256" -o "$TASK_DOWNLOAD/uv.sha256"
            TASK_EXPECTED="$(awk '{print $1}' "$TASK_DOWNLOAD/uv.sha256")"
            TASK_ACTUAL="$(shasum -a 256 "$TASK_DOWNLOAD/uv.tar.gz" | awk '{print $1}')"
            if [[ ${#TASK_EXPECTED} != 64 || "$TASK_EXPECTED" != "$TASK_ACTUAL" ]]; then
                printf '下载校验失败，请重新打开启动器。\n' >&2
                exit 1
            fi
            tar -xzf "$TASK_DOWNLOAD/uv.tar.gz" -C "$TASK_DOWNLOAD" uv-aarch64-apple-darwin/uv
            mkdir -p "$TASK_RUNTIME/bin"
            mv "$TASK_DOWNLOAD/uv-aarch64-apple-darwin/uv" "$TASK_UV"
        fi
    fi
    "$TASK_UV" python install --no-bin 3.11
    if ! "$TASK_PYTHON" -c \
        'import platform, sys; assert platform.machine() == "arm64" and sys.version_info[:2] == (3, 11)' 2>/dev/null; then
        "$TASK_UV" venv --quiet --clear --python 3.11 "$TASK_RUNTIME/venv"
    fi
    "$TASK_PYTHON" -c 'import platform, sys; assert platform.machine() == "arm64" and sys.version_info[:2] == (3, 11)'
    "$TASK_UV" pip install --python "$TASK_PYTHON" -r requirements.txt
    printf '%s\n' "$TASK_REQUIREMENTS" > "$TASK_RUNTIME/requirements.sha256"
fi

# The installer lock must not remain held while the menu or inference is running.
cleanup
trap - EXIT
exec "$TASK_PYTHON" -m tools.macos_launcher "$@"
