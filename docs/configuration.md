# 配置与运行方式

本文补充 README 中的高级配置。所有环境变量都写在 `.env`，Compose 会通过 `env_file` 传入容器。

## 认证与处理范围

三种认证方式只能启用一种：

```env
IMMICH_API_KEY=single-user-key
# IMMICH_API_KEYS=["key1","key2"]
# IMMICH_LIBRARIES={"Alice":"key1","Bob":"key2"}
```

`IMMICH_LIBRARIES` 只是兼容上游的名称，实际是“账号名称 → API Key”的映射，并不是图库配置。多账号运行时，每个账号使用相同的范围设置。

```env
IMMICH_INCLUDE_LIBRARY_IDS=library-uuid-1,library-uuid-2
IMMICH_INCLUDE_ALBUM_IDS=album-uuid-1
IMMICH_EXCLUDE_LIBRARY_IDS=library-uuid-3
SEARCH_API=auto
```

列表也可以写成 JSON 数组。图库 include 取并集，相册 include 取并集；两者同时设置时取交集；exclude 始终优先。没有 include 范围时会处理 API 用户可见的所有图片并打印警告。上传资产可能没有 `libraryId`，此时应通过相册范围限制。

程序只接受图片，并跳过离线、回收站和已删除资产。常规推理会跳过带有 `PROCESSED_TAG_NAME` 的资产；中文补全会检查已经处理过的资产。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `IMMICH_BASE_URL` | 必填 | Immich 根地址，不要包含 `/api` |
| `IMMICH_API_KEY` / `IMMICH_API_KEYS` / `IMMICH_LIBRARIES` | 必须三选一 | 单账号、多 Key 或命名账号 |
| `IMMICH_INCLUDE_LIBRARY_IDS` | 空 | 包含的外部图库 UUID |
| `IMMICH_INCLUDE_ALBUM_IDS` | 空 | 包含的相册 UUID |
| `IMMICH_EXCLUDE_LIBRARY_IDS` | 空 | 排除的外部图库 UUID |
| `SEARCH_API` | `auto` | `auto`、`structured` 或 `legacy` |
| `GENERAL_THRESHOLD` | `0.35` | 普通标签阈值；为空时沿用 `CONFIDENCE_THRESHOLD` |
| `CONFIDENCE_THRESHOLD` | `0.35` | 上游兼容名称 |
| `CHARACTER_THRESHOLD` | `0.90` | 角色标签阈值 |
| `BATCH_SIZE` | `250` | API 分页大小，范围 1–1000 |
| `PROCESSED_TAG_NAME` | `auto:processed` | 常规推理成功后的标记 |
| `FAILURE_TIMEOUT` | `3` | 同一资产失败达到次数后暂停重试；`0` 等同一次失败即暂停 |
| `TAGGING_MODEL` | `wd14` | `wd14` 或可选的 `deepdanbooru` |
| `MODEL_REPO` | `SmilingWolf/wd-swinv2-tagger-v3` | Hugging Face 模型仓库 |
| `MODEL_CACHE_DIR` | `/app/models` | 模型和词表缓存目录 |
| `DEEPDANBOORU_PROJECT_DIR` | 空 | DeepDanbooru 项目目录 |
| `STATE_DIR` | `/app/state` | SQLite 任务队列、失败记录、锁和写入日志 |
| `ENGLISH_TAGS_ENABLED` | `true` | 是否始终写入模型的英文标签；关闭后优先中文，缺少译名时使用英文 |
| `TRANSLATIONS_ENABLED` | `true` | 是否添加中文标签 |
| `TRANSLATION_OVERRIDES` | 空 | 自定义译名 JSON 文件 |
| `MAX_RETRIES` / `RETRY_DELAY` | `3` / `1` | HTTP 重试次数和退避秒数 |
| `REQUEST_TIMEOUT` | `30` | 单次 HTTP 请求超时秒数 |
| `TAG_CACHE_TTL` | `300` | 标签列表缓存秒数 |
| `LOG_LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL` |
| `LOG_PROGRESS_INTERVAL_SECONDS` | `10` | 运行状态输出间隔，正整数秒 |
| `LOG_SLOW_OPERATION_SECONDS` | `60` | 操作等待提示阈值，正整数秒；同一操作后续最多每 60 秒警告一次 |
| `HEALTH_PORT` | `8000` | 健康检查端口 |
| `ENABLE_SCHEDULER` | `true` | scheduler 模式是否循环等待下一轮 |
| `CRON_SCHEDULE` | `0 2 * * *` | cron 表达式 |
| `TIMEZONE` | `Asia/Shanghai` | 定时任务时区 |
| `RUN_ON_STARTUP` | `false` | 是否在 scheduler 启动时立即执行一轮 |
| `RESUME_ON_STARTUP` | `true` | scheduler 启动时优先恢复配置兼容的未完成任务 |

`BATCH_SIZE` 是搜索分页大小，不代表推理并发数。WD14 的评级标签使用专门阈值，普通标签使用 `GENERAL_THRESHOLD`。

标签语言可以按下表组合，至少启用一种输出：

| `ENGLISH_TAGS_ENABLED` | `TRANSLATIONS_ENABLED` | 新增标签 |
| --- | --- | --- |
| `true` | `true` | 英文 + 中文（默认） |
| `false` | `true` | 有译名时只写中文，缺少译名时写英文 |
| `true` | `false` | 仅英文 |
| `false` | `false` | 配置错误，启动时拒绝 |

语言开关只控制后续新增标签，不删除已有标签。常规推理仍使用 `PROCESSED_TAG_NAME` 完成标记，并跳过已处理资产；所有预测都缺少译名时，会写入英文标签并记录本次推理已完成。`backfill-zh` 不受英文输出开关影响，仍从资产已有的英文标签补中文。

## 运行模式

所有命令都可以放在 `docker compose run --rm immich-tagger` 后面：

| 模式 | 用途 |
| --- | --- |
| `single` | 最多处理一个 `BATCH_SIZE`，适合手动验证 |
| `continuous` | 处理本轮所有候选后退出 |
| `scheduler` | 按 cron 定时重复运行 |
| `backfill-zh` | 从已有英文标签补中文，不加载模型 |
| `health-only` | 只提供健康检查服务 |

附加参数：`--limit N` 限制所有账号合计处理数，`--batch-size N` 临时覆盖分页大小，`--library-id UUID` 临时指定包含图库，`--dry-run` 只预览标签计划。旧参数 `--max-cycles N` 仍可用，表示总上限 `N × BATCH_SIZE`。

`--test-connection` 只读取标签列表；`--show-failures`、`--reset-failure ASSET_ID` 和 `--reset-failures` 管理失败记录；`--progress-status` 只读查询最近一次持久化任务摘要，输出 `source=persisted`、`live=false`、`running=null`；实时状态使用 `/metrics`。没有数据库时显示尚无记录，不创建状态目录。`--reset-progress` 已弃用，不会重置计数或删除队列。

## 中文词典与自定义译名

词典覆盖默认 WD14 词表中的大部分普通和角色标签。运行时不访问翻译服务，缺失项保留英文并在日志中汇总，后续更新词典后可通过 `backfill-zh` 补中文。优先中文模式下，已有译名的条目不额外保存英文，后续更换这些条目的译名需要重新推理或手动整理。词典来源、固定版本和许可见 [data/NOTICE.md](../data/NOTICE.md)。

覆盖文件示例：

```json
{
  "blue_hair": {"zh": "蓝发", "kind": "general"},
  "hatsune_miku": {"zh": "初音未来", "kind": "character"},
  "explicit": {"zh": "成人", "kind": "rating"}
}
```

`kind` 只能是 `general`、`character` 或 `rating`。把 `zh` 设为空字符串可关闭该项的中文译名，推理时仍写入英文标签；中文文本中的半角 `/` 会转为全角 `／`，避免意外创建额外层级。覆盖文件挂载在 `state/` 中，更新镜像不会覆盖它。改译名后，补全模式会为仍有对应英文标签的资产增加新标签，旧标签需要在 Immich 中自行整理。

## 失败记录、写入与健康检查

只有标签关联回读确认成功后，才会添加处理标记。部分写入失败时，重试会保留已成功的标签并补齐缺项；模型初始化失败会终止整轮，不把所有图片记为失败。达到 `FAILURE_TIMEOUT` 后，资产只会被跳过，修复原因后使用重置命令恢复。

`state/progress.sqlite3` 保存候选队列、逐张结果和失败计数，`state/assignments.jsonl` 记录新增标签（账号只保存服务地址和 Key 的摘要），`state/writer.lock` 防止同一目录多进程同时写入。SQLite 使用 WAL，目录中的 `-wal`、`-shm` 文件属于数据库运行状态，不要在运行中删除。当前没有自动回滚命令。

健康检查默认绑定宿主机 `127.0.0.1:8000`，提供 `/health` 和 `/metrics`。它们读取线程安全的进程快照，不主动请求 Immich 或加载模型；最近一轮失败时 `/health` 返回 503。仅等待较久不判为故障。正常健康检查访问不再打印访问日志，健康状态变化会单独提示。

## 中文日志与实时进度

```bash
docker compose logs -f immich-tagger
curl -s http://127.0.0.1:8000/metrics
docker compose run --rm immich-tagger python -m immich_tagger.main --progress-status
```

日志时间使用 `TIMEZONE` 并带 UTC 偏移。阶段切换、重试和错误立即记录；运行时由独立报告线程定时显示状态，即使当前请求或模型调用尚未返回，也能看到操作名称和等待时间。默认 INFO 输出汇总，DEBUG 输出单张结果和标签明细；dry-run 始终在 INFO 显示计划添加的标签。

```text
2026-09-27 17:21:10+08:00 INFO [扫描] 已读取 12 页｜已读取记录 3000 条｜已选候选 2400 张｜补取详情 1850 次
2026-09-27 17:22:18+08:00 INFO [模型] 正在准备模型 SmilingWolf/wd-swinv2-tagger-v3｜首次运行可能需要下载权重
2026-09-27 17:24:40+08:00 INFO [处理] 已完成 120/4260（2.8%）｜成功 118｜失败 2｜跳过 0｜本次完成 120 张
```

`/metrics` 保留原有字段，增加 `progress`：

| 字段 | 含义 |
| --- | --- |
| `run_id` / `task_status` / `resumed` | 任务 ID、任务状态、是否续跑 |
| `phase` / `account` | 当前阶段和账号；阶段为 `idle`、`recovering`、`scanning`、`preparing_model`、`processing`、`stopping`、`paused`、`completed`、`error`、`waiting` |
| `scan_pages` / `scan_records` | 接受的搜索分页数及其返回记录数；不等同于全库唯一图片总数 |
| `detail_requests` / `candidates` / `scan_skips` | 补取详情次数、已选候选、本地跳过原因计数 |
| `total` / `completed` / `remaining` | 固定队列总量、已结束尝试数、剩余量；扫描结束前总量和剩余量为 `null` |
| `session_completed` | 本次执行完成数；恢复时从零开始 |
| `current_asset_id` / `operation` | 当前图片及操作代码，如 `search`、`read_details`、`load_model`、`download`、`inference`、`assign_tags`、`readback`、`checkpoint` |
| `phase_elapsed_seconds` / `operation_elapsed_seconds` / `session_elapsed_seconds` | 当前阶段、操作及本次执行耗时，停机时间不计入 |
| `assets_per_second` | 本次处理阶段完成数除以处理阶段耗时，排除扫描、模型准备和停机时间 |
| `last_progress_at` / `next_run_at` | 最近实际推进时间、下一轮计划时间；定时状态日志不会刷新推进时间 |

`completed` 包含队列中的成功、失败、预览和处理时跳过；扫描阶段被过滤的记录不计入百分比分母。`last_run` 继续保留原有计数语义，`skipped` 还包含扫描时因失败次数达到上限而排除的图片。业务提示中文，接口阶段和操作代码保持英文便于工具使用。模型准备显示步骤与等待时间，不伪造下载百分比。

## 重启续跑与配置变更

扫描已完成的任务在重启后使用持久队列继续处理，无需再次全库搜索。恢复会刷新相册成员，并在图片推理前和写入前读取资产最新状态；已有完成标记、已删除、离线或已移出范围的图片会跳过。单张写入完成但本地尚未提交便断电时，通过实际标签和完成标记核对；未完成图片可能重新推理，但只补缺少的标签。

扫描未完成的任务会重新扫描并按账号和图片 ID 去重：只保留本次重新确认的候选，不使用旧页码或旧游标直接续扫。整个队列固定后才开始写入。达到 `--limit` / `single` / `--max-cycles` 的合并上限也算本轮扫描完成；额度针对整轮，多次重启不会扩大额度。恢复中的新图片由下一轮扫描发现。

默认 `RESUME_ON_STARTUP=true`：兼容的未完成任务启动后立即恢复，恢复后不再额外执行 `RUN_ON_STARTUP` 新任务。设置为 false 只关闭立即恢复，下一次定时触发仍会优先续跑。`ENABLE_SCHEDULER=false` 只执行一轮，优先恢复；`health-only` 不恢复任务、不迁移状态、不调用 Immich。预览使用临时队列，不创建或更新正式恢复状态。

账号身份或顺序、图库/相册范围、处理类型、模型、阈值、语言输出、词典内容、完成标记、失败策略、有效处理上限发生变化时，旧任务标记为已替代，新任务重新扫描。已有 Immich 标签仍然保留。日志设置、时区、cron、请求超时不影响续跑；分页大小仅在影响有效总上限时使任务不兼容。自动打标签和中文补全任务独立。

成功、失败、跳过与失败次数在同一 SQLite 事务中提交；单张失败本轮不无限重试，下一轮按失败策略重试。模型、词典、鉴权、数据库等任务级错误保留队列，不把所有图片记为失败。SIGTERM 停止领取新图片并尽量完成当前图片；被强制终止后从最后已提交结果继续。已结束任务仅保留最近 100 条汇总，未完成队列不自动清理。

## 从旧版升级

1. 停止服务，完整备份状态目录，保留原镜像版本或 digest：

   ```bash
   docker compose stop immich-tagger
   cp -a state state.before-progress-upgrade
   ```

2. 更新镜像并启动，继续挂载原有 `state/` 和 `models/`。首次需要写状态时，旧 `failures-*.json` 在写入锁内一次性导入 SQLite，原文件保留作为备份。失败查询只读；导入后重置失败记录不会被旧 JSON 重新覆盖。
3. **旧版没有保存候选队列，第一次升级仍需扫描。** Immich 完成标记和模型缓存继续有效。从新版建立并固定的队列开始，后续重启即可续跑。

SQLite 已成为失败状态和任务队列的权威来源，不要继续修改备份 JSON。迁移失败、数据库损坏或版本不兼容会明确报错，不自动清空恢复数据。状态卷应使用支持本地文件锁的存储，备份时先停止写入服务并复制整个目录。

回退旧镜像时先停止新版，把当前 `state/` 整体另存，再从 `state.before-progress-upgrade` 恢复旧状态目录。旧版无法读取新队列；升级后的新标签仍保存在 Immich，不会因回退状态目录被撤销。

## Immich 兼容性

客户端优先使用带 `filter/orderBy/cursor` 的结构化搜索，自动模式在服务器返回 HTTP 400 或旧分页格式时回退到 `page/nextPage`。鉴权错误不会触发无范围回退。标签写入依赖 Immich 的 `PUT /api/tags` 和 `PUT /api/tags/assets`。
