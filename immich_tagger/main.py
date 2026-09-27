"""CLI entry point with no import-time configuration or model side effects."""
import argparse
import asyncio
import json
import logging
import signal

from .config import Settings
from . import __version__
from .logging import safe_error, setup_logging


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Value must be positive")
    return number


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Immich ACG tagging and offline Chinese translations")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--mode", choices=["single", "continuous", "scheduler", "health-only", "backfill-zh"], default="continuous")
    parser.add_argument("--limit", type=positive_integer, help="Total asset limit across all accounts")
    parser.add_argument("--batch-size", type=positive_integer)
    parser.add_argument("--max-cycles", type=positive_integer)
    parser.add_argument("--library-id", action="append", help="External library UUID; repeat to include several")
    parser.add_argument("--dry-run", action="store_true", help="Preview tag additions without any Immich or state writes")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--test-connection", action="store_true")
    commands.add_argument("--show-failures", action="store_true")
    commands.add_argument("--reset-failures", action="store_true")
    commands.add_argument("--reset-failure", metavar="ASSET_ID")
    commands.add_argument("--progress-status", action="store_true")
    commands.add_argument("--reset-progress", action="store_true", help="已弃用：此参数不重置持久化进度或删除队列")
    return parser.parse_args(argv)


async def run_service(processor, args):
    from .health_server import HealthServer
    from .scheduler import Scheduler
    scheduler = Scheduler(processor) if args.mode == "scheduler" else None
    shutdown = asyncio.Event()

    def stop():
        processor.cancelled.set()
        processor.progress.phase("stopping", "收到停止信号，停止领取新图片，等待当前操作结束")
        shutdown.set()
        if scheduler:
            scheduler.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    health = HealthServer(processor)
    try:
        await health.start()
        logging.getLogger("main").info("[启动] 服务已启动｜版本：%s｜模式：%s｜时区：%s｜启动新任务：%s｜启动恢复：%s",
                                       __version__,
                                       {"single": "单批", "continuous": "本轮全部", "scheduler": "定时任务",
                                        "backfill-zh": "补充中文标签", "health-only": "仅健康检查"}[args.mode],
                                       processor.settings.timezone,
                                       "是" if processor.settings.run_on_startup else "否",
                                       "是" if processor.settings.resume_on_startup else "否")
        kwargs = {"limit": args.limit, "max_cycles": args.max_cycles}
        if scheduler:
            return await scheduler.start(**kwargs)
        if args.mode == "health-only":
            await shutdown.wait()
            return 0
        result = await asyncio.to_thread(
            processor.run, backfill=args.mode == "backfill-zh",
            single=args.mode == "single", **kwargs,
        )
        if shutdown.is_set():
            return 130
        return 1 if result.failed else 0
    finally:
        await health.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv=None):
    args = parse_arguments(argv)
    setup_logging()
    processor = None
    secrets = []
    try:
        overrides = {}
        if args.batch_size is not None:
            overrides["batch_size"] = args.batch_size
        if args.library_id:
            overrides["immich_include_library_ids"] = args.library_id
        settings = Settings(**overrides)
        secrets = [account["api_key"] for account in settings.get_library_config()]
        setup_logging(settings.log_level, settings.timezone, secrets)
        if args.dry_run and (args.reset_failures or args.reset_failure or args.reset_progress):
            raise ValueError("Dry run cannot be combined with state reset commands")
        if args.dry_run and args.mode in ("scheduler", "health-only"):
            raise ValueError("Dry run requires single, continuous or backfill-zh mode")
        if args.progress_status:
            from .task_store import TaskStore
            print(json.dumps(TaskStore.snapshot(settings.state_dir), ensure_ascii=False))
            return 0
        if args.reset_progress:
            print("该参数已弃用：进度已持久化，此参数不重置计数或删除未完成队列")
            return 0
        from .processor import ImmichAutoTagger
        processor = ImmichAutoTagger(settings, dry_run=args.dry_run)
        if args.test_connection:
            processor.test_connection()
            print("连接成功")
            return 0
        if args.show_failures or args.reset_failures or args.reset_failure:
            summaries = {}
            for client in processor.clients:
                summaries[client.current_library_name] = {}
                for backfill in (False, True):
                    tracker = processor.failure_tracker(client, backfill)
                    if args.reset_failures or args.reset_failure:
                        tracker.reset_failures([args.reset_failure] if args.reset_failure else None)
                    summaries[client.current_library_name]["translation" if backfill else "inference"] = tracker.get_failure_summary()
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
            return 0
        return asyncio.run(run_service(processor, args))
    except Exception as error:
        logging.getLogger("main").error("[异常] %s", safe_error(error, secrets))
        return 1
    finally:
        if processor:
            processor.close()


if __name__ == "__main__":
    raise SystemExit(main())
