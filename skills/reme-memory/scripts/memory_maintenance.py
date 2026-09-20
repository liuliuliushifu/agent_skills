#!/usr/bin/env python3
import argparse
import datetime as dt
import json

from memory_bus_client import enqueue_maintenance, wait_for_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ReMe fallback cleanup and compact 30/90 maintenance through the daemon.")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="Maintenance date used for idempotency")
    parser.add_argument("--compact-active-days", type=int, default=30)
    parser.add_argument("--compact-delete-days", type=int, default=90)
    parser.add_argument("--skip-fallback-cleanup", action="store_true")
    parser.add_argument("--skip-compact-retention", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=300.0)
    args = parser.parse_args()

    accepted = enqueue_maintenance(
        maintenance_date=args.date,
        cleanup_fallback=not args.skip_fallback_cleanup,
        compact_retention=not args.skip_compact_retention,
        compact_active_days=args.compact_active_days,
        compact_delete_days=args.compact_delete_days,
        dry_run=args.dry_run,
        client_id="reme-maintenance",
    )
    result = wait_for_result(accepted.request_id, timeout_seconds=args.wait_timeout)
    print(
        json.dumps(
            {
                "status": result.get("status", "stored"),
                "accepted": accepted.to_dict(),
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
