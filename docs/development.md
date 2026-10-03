# 开发与测试

## 目录与入口

```text
app/            主程序，内部按模块平铺
tests/          自动化测试；conftest.py 仅提供 pytest fixtures
tests/support/  公共模拟对象、测试数据构造函数和重启辅助程序
tools/          独立维护工具，不进入运行镜像
scripts/        词典构建与容器验收脚本
data/           随程序发布的词典及来源许可
docs/           配置、开发与发布说明
models/         本地模型缓存，不纳入版本管理
state/          本地状态与审计记录，不纳入版本管理
```

所有开发命令从项目根目录执行。自 `1.2.0` 起程序入口为 `python -m app.main`，导入使用 `app.*`；`1.1.x` 旧镜像仍使用旧入口，镜像选择见 [README](../README.md#从源码构建)。

## 本地环境

项目使用 Python 3.11。只运行测试、连接检查、中文补全或英文清理时，安装核心测试依赖即可：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt
python -m app.main --help
python -m pytest -q
```

本地推理还需要安装 `requirements.txt`。Linux CPU 环境可先安装 CPU 版 PyTorch，避免拉取 CUDA 依赖：

```bash
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 torchvision==0.21.0
pip install -r requirements.txt
```

本地 `.env` 应把 `MODEL_CACHE_DIR` 和 `STATE_DIR` 指向可写目录，例如 `models` 和 `state`。不要提交 `.env.local`、模型权重或状态文件。

macOS ARM64 从 PyPI 安装 `requirements.txt`，无需使用 Linux CPU wheel 索引。原生 MPS、launchd 与本地概率/性能对比见 [macOS 文档](macos.md)。离线 CPU 契约检查：`python -m scripts.check_inference_runtime`，使用真实库和随机初始化 SwinV2 权重，不下载模型、不调用 Immich。

## 测试和容器构建

```bash
python -m pytest -q
python -m compileall -q app tests tools scripts
docker build --target test -t immich-booru-tagger:test .
docker run --rm --network none immich-booru-tagger:test
```

测试使用模拟 Immich API，覆盖配置校验、结构化/旧版搜索、图库与相册范围、标签写入回读、中文补全、英文清理、失败重试和健康服务。真实 Immich 验收仍应按 README 的连接测试、小批量预览和小批量写入顺序进行。

2026-09-27 在一套 Immich 3.2.2 实例完成受限英文清理验收：`recorded` 预览 5 张、`catalog` 预览 1 张均无写入；实际只处理 1 张图片的 15 个英文关联，保留中文、完成标记和无译名标签，重复运行无额外删除。测试中观察到删除后英文重新出现；加入至少 5 秒的逐项间隔、两次整张延迟复核及有限重试后，共 22 次解除请求最终通过。测试结束恢复原标签，延迟核验 5 张图片及全局标签均与基线一致。本地与 Linux AMD64 容器均通过 157 项测试。这是单实例、单张写入样本，不代表任意服务器负载下都能及时完成；全量操作前仍应小批量验证。

2026-09-28 在隔离的真实 Immich 3.2.2 中验证维护模式：使用实际清理器分别从两队列运行、sidecar 暂停、metadataExtraction 暂停三种状态开始；15 个英文关联在 2.653–2.689 秒完成（旧代码同图 86.436 秒），17 个保留标签精确一致，主动刷新元数据后无回流，队列恢复原始暂停状态。本地通过 185 项自动化测试；维护模式新增覆盖队列权限失败、控制响应丢失、超时、取消、失败复核、恢复记录及 SIGKILL 续跑。测试图片为隔离实例中的合成 JPEG，未修改线上图库。

同日后续故障复现发现：上述批量解除方式仍可能造成同图 XMP 并发冲突；只读图库也可能因服务端吞掉文件错误而丢失保留标签。当前源码增加完整标签基线及逐项 sidecar 排空后，本地和 Linux AMD64 容器均通过 227 项测试，包括基线损坏/写入失败、改范围续跑、普通写入阻断与强制中断恢复；离线容器 stop/kill/scan 恢复及运行镜像健康检查通过。在独立 Immich 3.2.2、sidecar 并发为默认 5 的合成图片验收中，三种空图片均跳过且无标签/队列写入，上传图片和外部图库共 7 轮均为 32 → 17，保留标签精确一致，主动刷新元数据后无回流；每轮清理加刷新验证约 16.3–17.8 秒。该阶段日志未出现 XMP 临时文件冲突。只读样本仍为 6 → 0，但首次及后续两次运行（含更改上限）均中止且原基线保持不变，普通打标和补全也被阻止。此结果验证客户端保护与并发缓解，不代表服务端文件读写故障已修复；测试完成停止隔离实例，保留测试卷和证据，未连接生产实例写入。

## 模块职责

以下模块均位于 `app/`，从 `main.py` 的命令行解析与服务组装开始阅读。

- `config.py`：环境变量解析、认证互斥和 UUID 校验。
- `immich_client.py`：Immich API、搜索回退、标签缓存、写入回读和下载预览图。
- `processor.py`：快照、范围检查、推理/中文补全、失败记录和完成标记。
- `english_cleanup.py`：历史记录/词典筛选、逐项解关联和删除前清单，复用处理器队列与锁。
- `cleanup_queues.py`：持久化队列维护状态，协调 XMP 写入与元数据提取，支持中断后独立恢复。
- `cleanup_state.py`：持久化单张清理前的完整标签基线，独立于队列恢复核验保留标签，确认后归档；未复核的图片阻止后续写入。
- `tagging_engine.py`：WD14 默认后端与可选 DeepDanbooru 后端。
- `translation_catalog.py`：离线词典、覆盖项和 `属性/`、`角色/`、`评级/` 路径生成。
- `state.py`：单写入锁和追加式写入记录。
- `task_store.py`：SQLite 任务队列、失败迁移和逐张原子检查点。
- `progress.py`：线程安全状态、中文阶段日志和独立报告线程。
- `health_server.py`：基于进程快照的 `/health` 与 `/metrics`。

处理前会先保存候选快照，再执行写入，避免旧版按页搜索在标签写入后改变分页结果。常规模式只把 `auto:processed` 作为完成标记；中文补全和英文清理不添加这个标记。清理使用独立任务类型及失败命名空间，必须显式命令才能执行或恢复。

## 词典构建

词典生成脚本从 `data/NOTICE.md` 中记录的固定翻译数据库和 `wdtagger==0.16.0` wheel 读取输入，并把来源修订号、输入哈希和覆盖数量写入输出：

```bash
python scripts/build_catalog.py \
  --database /path/to/tags.sqlite \
  --labels-wheel /path/to/wdtagger-0.16.0-py3-none-any.whl
```

修改词典前要检查来源许可、标签类别和覆盖率变化。评级词条由脚本补充，输出路径必须保持为 `data/tag_translations.json`。

## 可选 DeepDanbooru

DeepDanbooru 不装进默认 CPU 镜像，也不会自动下载项目目录。在独立环境安装 `requirements-deepdanbooru.txt`，设置 `TAGGING_MODEL=deepdanbooru` 与 `DEEPDANBOORU_PROJECT_DIR` 后再运行。该后端目前只有代码适配，未纳入真实模型验收。

## 独立资产清理工具

`tools/cleanup_failed_assets.py` 是删除失败资产的独立工具，与 `app/english_cleanup.py` 的英文标签清理不同，不打包进运行镜像。识别失败时应先排查模型、网络和图片格式。从项目根目录查看帮助或预览：

```bash
python -m tools.cleanup_failed_assets --help
python -m tools.cleanup_failed_assets --dry-run
```

## 中断恢复验收

`tests/test_process_restart.py` 启动真实子进程，在扫描、模型准备、推理、写入标签、写入完成标记和本地提交前后发送 SIGTERM / SIGKILL，使用同一数据库恢复并校验无重复写入、无需重新扫描及累计额度。

公共模拟对象位于 `tests/support/fakes.py`，测试直接从 `tests.support.fakes` 导入。重启辅助程序在本地和容器中均通过 `python -m tests.support.restart_harness` 启动；本地子进程的工作目录显式设为项目根目录。

同一子进程测试也覆盖清理扫描、日志落盘、远程删除成功和本地提交前后的 SIGKILL，验证恢复依据完整、不会重复删除且不会加载模型。维护模式另覆盖恢复意图落盘、两队列分别暂停、远程删除、两队列分别恢复及本地检查点前后的 8 个 SIGKILL 场景，验证原始暂停状态恢复且复核前不提交成功。

容器级验收使用模拟 Immich 和模型，容器无外部网络，不涉及真实账号：

```bash
docker build --target test -t immich-booru-tagger:progress-test .
python scripts/check_container_restart.py --image immich-booru-tagger:progress-test
```

脚本创建临时挂载目录与随机命名的测试容器，分别验证正常停止、强制结束、扫描中断后重建容器续跑；结束时仅清理本次创建的测试资源。实时 `/metrics` 在阻塞期间也会被检查。发布流程分别在原生 AMD64/ARM64 runner 上构建、测试及执行离线模型依赖冒烟检查，两个架构成功后合并 manifest。实体 MPS 仍需单独验收。
