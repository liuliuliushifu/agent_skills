#!/usr/bin/env python3
import asyncio
import json
import sys
from typing import Any, Dict

from runtime_session import RuntimeSession


def _write_response(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    session = RuntimeSession()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            request = json.loads(line)
            task_id = request.get("task_id", "")
            op = request.get("op", "")
            kwargs = request.get("kwargs", {}) or {}
            try:
                if op == "shutdown":
                    result = loop.run_until_complete(session.close())
                    _write_response({"task_id": task_id, "ok": True, "result": result})
                    break
                if op == "health":
                    result = loop.run_until_complete(session.health_snapshot())
                elif op == "search":
                    result = loop.run_until_complete(session.search(**kwargs))
                elif op == "upsert_memory_file":
                    result = loop.run_until_complete(session.upsert_memory_file(**kwargs))
                elif op == "delete_memory_file":
                    result = loop.run_until_complete(session.delete_memory_file(**kwargs))
                elif op == "sync_memory_files":
                    result = loop.run_until_complete(session.sync_memory_files(**kwargs))
                else:
                    raise ValueError(f"unknown worker op: {op}")
            except Exception as exc:
                _write_response(
                    {
                        "task_id": task_id,
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                continue
            _write_response({"task_id": task_id, "ok": True, "result": result})
    finally:
        try:
            loop.run_until_complete(session.close())
        except Exception:
            pass
        loop.close()


if __name__ == "__main__":
    main()
