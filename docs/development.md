# 开发与测试

## 本地环境

项目使用 Python 3.11。只运行测试、连接检查、中文补全或英文清理时，安装核心测试依赖即可：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt
python -m pytest -q
```

本地推理还需要安装 `requirements.txt`。Linux CPU 环境可先安装 CPU 版 PyTorch，避免拉取 CUDA 依赖：

```bash
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 torchvision==0.21.0
pip install -r requirements.txt
```

本地 `.env` 应把 `MODEL_CACHE_DIR` 和 `STATE_DIR` 指向可写目录，例如 `models` 和 `state`。不要提交 `.env.local`、模型权重或状态文件。

## 测试和容器构建

```bash
python -m pytest -q
python -m compileall -q immich_tagger
docker build --target test -t immich-booru-tagger:test .
docker run --rm --network none immich-booru-tagger:test
```

测试使用模拟 Immich API，覆盖配置校验、结构化/旧版搜索、图库与相册范围、标签写入回读、中文补全、英文清理、失败重试和健康服务。真实 Immich 验收仍应按 README 的连接测试、小批量预览和小批量写入顺序进行。

2026-09-27 在一套 Immich 3.2.2 实例完成受限英文清理验收：`recorded` 预览 5 张、`catalog` 预览 1 张均无写入；实际只处理 1 张图片的 15 个英文关联，保留中文、完成标记和无译名标签，重复运行无额外删除。测试中观察到删除后英文重新出现；加入至少 5 秒的逐项间隔、两次整张延迟复核及有限重试后，共 22 次解除请求最终通过。测试结束恢复原标签，延迟核验 5 张图片及全局标签均与基线一致。本地与 Linux AMD64 容器均通过 157 项测试。这是单实例、单张写入样本，不代表任意服务器负载下都能及时完成；全量操作前仍应小批量验证。

## 模块职责

- `config.py`：环境变量解析、认证互斥和 UUID 校验。
- `immich_client.py`：Immich API、搜索回退、标签缓存、写入回读和下载预览图。
- `processor.py`：快照、范围检查、推理/中文补全、失败记录和完成标记。
- `cleanup.py`：历史记录/词典筛选、逐项解关联和删除前清单，复用处理器队列与锁。
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

根目录的 `cleanup_failed_assets.py` 是独立的资产清理工具，不属于标签处理流程，也不打包进运行镜像。识别失败时应先排查模型、网络和图片格式。

## 中断恢复验收

`tests/test_process_restart.py` 启动真实子进程，在扫描、模型准备、推理、写入标签、写入完成标记和本地提交前后发送 SIGTERM / SIGKILL，使用同一数据库恢复并校验无重复写入、无需重新扫描及累计额度。

同一子进程测试也覆盖清理扫描、日志落盘、远程删除成功和本地提交前后的 SIGKILL，验证恢复依据完整、不会重复删除且不会加载模型。

容器级验收使用模拟 Immich 和模型，容器无外部网络，不涉及真实账号：

```bash
docker build --target test -t immich-booru-tagger:progress-test .
python scripts/check_container_restart.py --image immich-booru-tagger:progress-test
```

脚本创建临时挂载目录与随机命名的测试容器，分别验证正常停止、强制结束、扫描中断后重建容器续跑；结束时仅清理本次创建的测试资源。实时 `/metrics` 在阻塞期间也会被检查。生产发布仍使用既有 `linux/amd64` runtime 构建及离线模型依赖冒烟检查。
