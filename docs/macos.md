# macOS ARM64 原生运行

WD14 使用原生 PyTorch MPS 调用 Apple GPU，M4/M5 等 Apple Silicon 使用同一 ARM64/MPS 代码路径，无需机型专用配置。普通 Docker Desktop/OrbStack Linux 容器只使用 CPU；CPU 容器支持 AMD64 和 ARM64。MPS 不使用 Neural Engine。

## 双击启动（推荐）

无需提前安装 Python、Homebrew 或 Docker。

1. 下载源码 ZIP 并解压到固定目录，双击根目录的 **`启动.command`**。macOS 如拦截打开，可在“系统设置 → 隐私与安全性”允许这次打开。
2. 等待启动器自动准备 ARM64 Python 3.11 与依赖。首次安装需要联网，进度显示在窗口中；后续启动复用环境。
3. 按提示填写 Immich 地址、API Key 与处理范围；相册可从名称列表选择，无需查 UUID。启动器检查连接与相册接口。API Key 的输入不会显示；权限要求见 [README](../README.md#1-准备-api-key)。
4. 先查看最多 20 张图片的预览，再在菜单选择“开始处理／继续上次任务”。首次预览会下载模型，实际写入由用户选择启动。

以后双击同一个文件即可预览、续跑、改配置、开启或关闭登录后定时运行、查看状态和日志。“高级设置”可切换 `auto/cpu/mps`、CPU 线程数和标签语言。默认 `auto` 在可用时选择 MPS；MPS 故障暂停后，可切换 CPU 继续原队列。

启动器使用项目内的 `.macos/` 管理环境，配置写入权限为 `0600` 的 `.env.macos`。已有 `.env` 会作为首次配置的来源，容器路径自动转换为原生路径；原 `.env` 与 `.venv` 保留。默认以相对路径复用项目的 `models/` 与 `state/`，不会自动删除模型或进度。不要同时运行原生进程与使用同一状态目录的容器。

登录后定时运行由菜单生成用户级 launchd 配置，无需手写 plist；默认每天 02:00（配置中的时区），可在开启时选择时间。不启用无限自动重启；开启时及下次登录会优先恢复未完成队列。关闭菜单会卸载任务，保留配置与数据。修改配置或手动处理前先关闭定时服务。前台处理可按 Ctrl+C，等待当前图片操作完成后返回菜单。

前台日志在 `logs/macos-run.log`，后台日志在 `logs/macos-scheduler.log`。本地状态端口首次自动避开占用，也可在高级设置调整。安装失败后可再次双击重试。已开启定时运行的目录需要保持原位置；移动前先关闭定时任务，移动后重新开启。手动配置的绝对数据路径需要自行检查。机器休眠期间不保证按时执行。

## 高级：手动安装与启动

使用原生 ARM64 Python 3.11，避免 Rosetta 下的 x86_64 解释器。`uname -m` 与 Python 的 `platform.machine()` 均应为 `arm64`。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
mkdir -p models state logs
```

在 `.env` 中填写 Immich 地址、API Key 和测试相册范围，并修改容器路径：

```env
MODEL_CACHE_DIR=models
STATE_DIR=state
TAGGING_DEVICE=mps
# 可选；不设置时保留 PyTorch 默认线程数
# TAGGING_CPU_THREADS=4
```

从仓库根目录运行：

```bash
.venv/bin/python -m app.main --test-connection
.venv/bin/python -m app.main --dry-run --limit 20
# 核对预览后再写入
.venv/bin/python -m app.main --limit 20
.venv/bin/python -m app.main --mode scheduler
```

`auto` 按 CUDA → MPS → CPU 检测设备，不保证选择最快设备；可以明确指定 `cpu`。MPS 不可用、算子失败或内存不足时，任务暂停且不增加图片失败次数。改为 `TAGGING_DEVICE=cpu` 后使用同一状态目录与原处理上限重新运行即可续跑。不要同时启动容器和原生进程写入同一状态目录。

PyTorch 2.6 的安装必须支持 MPS，实际可用性以 `torch.backends.mps.is_available()` 为准。首期只验收默认 WD14 模型，其他兼容模型会先预热，失败时明确报错。保持 FP32；不启用 AMP、fast math 或算子 CPU 回退。`PYTORCH_ENABLE_MPS_FALLBACK=1` 仅供诊断，不能作为完整 GPU 验收。

## 高级：手动配置 launchd

将下面示例中的全部 `/ABSOLUTE/PATH/immich-booru-tagger` 替换为实际仓库路径，保存为 `~/Library/LaunchAgents/io.github.mikusaa.immich-booru-tagger.plist`。先创建日志目录并完成预览；`.env` 由工作目录读取，无需将 Key 放入 plist。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.github.mikusaa.immich-booru-tagger</string>
  <key>ProgramArguments</key>
  <array>
    <string>/ABSOLUTE/PATH/immich-booru-tagger/.venv/bin/python</string>
    <string>-m</string><string>app.main</string>
    <string>--mode</string><string>scheduler</string>
  </array>
  <key>WorkingDirectory</key><string>/ABSOLUTE/PATH/immich-booru-tagger</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>/ABSOLUTE/PATH/immich-booru-tagger/logs/stdout.log</string>
  <key>StandardErrorPath</key><string>/ABSOLUTE/PATH/immich-booru-tagger/logs/stderr.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.mikusaa.immich-booru-tagger.plist
# 停止，修改 .env 后可重新 bootstrap
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/io.github.mikusaa.immich-booru-tagger.plist
```

采用登录时启动与进程内调度，不设置无限重启。健康端点默认为 `http://127.0.0.1:8000/health`；端口占用时设置 `HEALTH_PORT`。机器休眠期间不保证定时运行，唤醒不保证补执行错过的 cron。

## 本地性能、正确性与稳定性

准备至少 100 张固定图片，包含透明 PNG、EXIF 旋转、不同尺寸。先缓存默认模型，再用 `--offline` 保证各次运行使用同一快照。工具不请求 Immich，报告和概率 NPZ 写入指定目录。

```bash
.venv/bin/python -m tools.benchmark_inference \
  --images /absolute/path/images --device cpu --cpu-threads 2 \
  --offline --output /tmp/ibt-benchmark/cpu-2.json
.venv/bin/python -m tools.benchmark_inference \
  --images /absolute/path/images --device mps --offline \
  --iterations 1000 --stability --reference /tmp/ibt-benchmark/cpu-2.json \
  --output /tmp/ibt-benchmark/mps.json
```

再分别用 `--cpu-threads 1`、`4` 和 ARM64 容器 CPU 比较。容器运行 benchmark 时需要挂载本地 `tools/`（默认运行镜像不包含工具）、同一图片目录、模型缓存与输出目录。

固定五次额外预热，冷启动时间包含加载及引擎准备预热，单独报告。测量包含解码、预处理、推理、CPU 回传与标签筛选，排除读取文件、完整概率采集、内存采样和 Immich API 耗时；GPU 在计时边界同步。

正确性比较全部类别概率：最大绝对误差 ≤ `1e-3`；标签集合差异只能发生在 CPU 基线阈值距离 ≤ `1e-3` 的条目，逐项列出图片索引、标签和概率；评级必须一致。基线必须是相同图片哈希、模型快照、词表及阈值的 CPU 报告，比较失败返回非零。

`--stability` 要求至少 1000 次。报告所有 RSS、MPS allocated/driver 内存样本；在后半程比较开头/末尾至多 100 个样本的中位数，增长不得超过起始值的 10% 或 32 MiB（取较大者）。这只是内存平台期筛查，仍应审查曲线；缺少 RSS 样本或开启 MPS fallback/fast math 不通过稳定性验收。不得以少量图片反复运行代替至少 100 张图片的正确性验收。

实体机与容器、不同设备的测试应串行执行，并记录电源、温度与后台负载。性能报告只能代表报告中的芯片和样本。macOS CPU CI 与随机权重冒烟检查不能替代 Apple Silicon 实机默认权重验收。本次采用 M5 实测证据，M4 尚未单独实测；实际结果见 [推理验收记录](inference-validation.md)。
