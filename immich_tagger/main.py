"""CLI entry point with no import-time configuration or model side effects."""
import argparse
import asyncio
import json
import logging
import signal

from .config import Settings
from .logging import setup_logging


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Value must be positive")
    return number


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Immich ACG tagging and offline Chinese translations")
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
    commands.add_argument("--reset-progress", action="store_true", help="Deprecated: progress is reported per process")
    return parser.parse_args(argv)


async def run_service(processor, args):
    from .health_server import HealthServer
    from .scheduler import Scheduler
    scheduler = Scheduler(processor) if args.mode == "scheduler" else None
    shutdown = asyncio.Event()

    def stop():
        processor.cancelled.set()
        shutdown.set()
        if scheduler:
            scheduler.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    health = HealthServer(processor)
    try:
        await health.start()
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
    try:
        overrides = {}
        if args.batch_size is not None:
            overrides["batch_size"] = args.batch_size
        if args.library_id:
            overrides["immich_include_library_ids"] = args.library_id
        settings = Settings(**overrides)
        setup_logging(settings.log_level)
        if args.dry_run and (args.reset_failures or args.reset_failure or args.reset_progress):
            raise ValueError("Dry run cannot be combined with state reset commands")
        if args.dry_run and args.mode in ("scheduler", "health-only"):
            raise ValueError("Dry run requires single, continuous or backfill-zh mode")
        from .processor import ImmichAutoTagger
        from .state import writer_lock
        processor = ImmichAutoTagger(settings, dry_run=args.dry_run)
        if args.test_connection:
            processor.test_connection()
            print("Connection successful")
            return 0
        if args.show_failures or args.reset_failures or args.reset_failure:
            summaries = {}
            readonly = args.show_failures
            with writer_lock(settings.state_dir, dry_run=readonly):
                for client in processor.clients:
                    summaries[client.current_library_name] = {}
                    for backfill in (False, True):
                        tracker = processor.failure_tracker(client, backfill)
                        if args.reset_failures or args.reset_failure:
                            tracker.reset_failures([args.reset_failure] if args.reset_failure else None)
                        summaries[client.current_library_name]["translation" if backfill else "inference"] = tracker.get_failure_summary()
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
            return 0
        if args.progress_status:
            print(json.dumps(processor.get_metrics(), ensure_ascii=False))
            return 0
        if args.reset_progress:
            print("Progress counters are per process; there is no persisted counter to reset")
            return 0
        return asyncio.run(run_service(processor, args))
    except Exception as error:
        logging.getLogger("main").error("%s", error)
        return 1
    finally:
        if processor:
            processor.close()


if __name__ == "__main__":
    raise SystemExit(main())
