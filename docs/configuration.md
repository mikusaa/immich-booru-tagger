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
| `STATE_DIR` | `/app/state` | 失败记录、锁和写入日志 |
| `TRANSLATIONS_ENABLED` | `true` | 是否添加中文标签 |
| `TRANSLATION_OVERRIDES` | 空 | 自定义译名 JSON 文件 |
| `MAX_RETRIES` / `RETRY_DELAY` | `3` / `1` | HTTP 重试次数和退避秒数 |
| `REQUEST_TIMEOUT` | `30` | 单次 HTTP 请求超时秒数 |
| `TAG_CACHE_TTL` | `300` | 标签列表缓存秒数 |
| `LOG_LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL` |
| `HEALTH_PORT` | `8000` | 健康检查端口 |
| `ENABLE_SCHEDULER` | `true` | scheduler 模式是否循环等待下一轮 |
| `CRON_SCHEDULE` | `0 2 * * *` | cron 表达式 |
| `TIMEZONE` | `Asia/Shanghai` | 定时任务时区 |
| `RUN_ON_STARTUP` | `false` | 是否在 scheduler 启动时立即执行一轮 |

`BATCH_SIZE` 是搜索分页大小，不代表推理并发数。WD14 的评级标签使用专门阈值，普通标签使用 `GENERAL_THRESHOLD`。

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

`--test-connection` 只读取标签列表；`--show-failures`、`--reset-failure ASSET_ID` 和 `--reset-failures` 管理失败记录；`--progress-status` 输出当前进程指标。

## 中文词典与自定义译名

词典覆盖默认 WD14 词表中的大部分普通和角色标签。运行时不访问翻译服务，缺失项保留英文并在日志中汇总。词典来源、固定版本和许可见 [data/NOTICE.md](../data/NOTICE.md)。

覆盖文件示例：

```json
{
  "blue_hair": {"zh": "蓝发", "kind": "general"},
  "hatsune_miku": {"zh": "初音未来", "kind": "character"},
  "explicit": {"zh": "成人", "kind": "rating"}
}
```

`kind` 只能是 `general`、`character` 或 `rating`。把 `zh` 设为空字符串可关闭该项；中文文本中的半角 `/` 会转为全角 `／`，避免意外创建额外层级。覆盖文件挂载在 `state/` 中，更新镜像不会覆盖它。改译名后，补全模式会增加新标签，旧标签需要在 Immich 中自行整理。

## 失败记录、写入与健康检查

只有标签关联回读确认成功后，才会添加处理标记。部分写入失败时，重试会保留已成功的标签并补齐缺项；模型初始化失败会终止整轮，不把所有图片记为失败。达到 `FAILURE_TIMEOUT` 后，资产只会被跳过，修复原因后使用重置命令恢复。

`state/failures-*.json` 保存失败计数，`state/assignments.jsonl` 记录新增标签（账号只保存服务地址和 Key 的摘要），`state/writer.lock` 防止同一目录多进程同时写入。当前没有自动回滚命令。

健康检查默认绑定宿主机 `127.0.0.1:8000`，提供 `/health` 和 `/metrics`。它们读取进程快照，不主动请求 Immich 或加载模型；最近一轮失败时 `/health` 返回 503。

## Immich 兼容性

客户端优先使用带 `filter/orderBy/cursor` 的结构化搜索，自动模式在服务器返回 HTTP 400 或旧分页格式时回退到 `page/nextPage`。鉴权错误不会触发无范围回退。标签写入依赖 Immich 的 `PUT /api/tags` 和 `PUT /api/tags/assets`。
