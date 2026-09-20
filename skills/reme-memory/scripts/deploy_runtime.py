#!/usr/bin/env python3
import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_RUNTIME_ROOT = Path(os.environ.get("REME_RUNTIME_ROOT", str(CODEX_HOME / "memories/reme-memory/daemon")))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def deploy_runtime(runtime_root: Path) -> dict:
    runtime_root = runtime_root.resolve()
    runtime_scripts = runtime_root / "scripts"
    runtime_scripts.mkdir(parents=True, exist_ok=True)

    deployed_files = []
    for source in sorted(SCRIPT_DIR.glob("*.py")):
        target = runtime_scripts / source.name
        shutil.copy2(source, target)
        deployed_files.append(str(target))

    manifest = {
        "source_skill_root": str(SKILL_ROOT),
        "source_scripts_root": str(SCRIPT_DIR),
        "runtime_root": str(runtime_root),
        "deployed_at": now_iso(),
        "deployed_files": deployed_files,
    }
    manifest_path = runtime_root / "deploy_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manifest_path": str(manifest_path), "deployed_files": deployed_files}


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy global reme-memory skill scripts to the runtime daemon copy.")
    parser.add_argument(
        "--runtime-root",
        default=str(DEFAULT_RUNTIME_ROOT),
        help="Runtime daemon root directory",
    )
    args = parser.parse_args()

    result = deploy_runtime(Path(args.runtime_root))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
