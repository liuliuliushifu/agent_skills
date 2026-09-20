#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List

from reme_runtime import normalize_embedding_base_url


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SEARCH_SCRIPT = SCRIPT_DIR / "search_memory.py"
REBUILD_SCRIPT = SCRIPT_DIR / "rebuild_index.py"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = Path(os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme")))
PYTHON = os.environ.get("REME_PYTHON", str(REME_HOME / ".venv/bin/python"))


def _run(cmd: List[str], env: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _write_memory_samples(memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)

    (memory_dir / "2026-04-20.md").write_text(
        """# Memory - 2026-04-20

- HostSync 慢启动处理：
  当慢启动阶段出现确认竞争时，优先增加重试次数，并补充时间日志定位问题，不要先修改协议。
""",
        encoding="utf-8",
    )

    (memory_dir / "2026-04-21.md").write_text(
        """# Memory - 2026-04-21

- ProvisionDB 高频通知原则：
  高频批量通知不要让 worker 反查 ProvisionDB，协议线程应该直接下发最终 apply item。
""",
        encoding="utf-8",
    )

    (memory_dir / "2026-04-22.md").write_text(
        """# Memory - 2026-04-22

- Build 技巧：
  SDK 编译失败时，先清理临时工作区和历史日志，再重新执行构建脚本。
""",
        encoding="utf-8",
    )


def _write_compact_memory_samples(compact_memory_dir: Path) -> None:
    compact_memory_dir.mkdir(parents=True, exist_ok=True)
    (compact_memory_dir / "2026-04-23-cap_test.md").write_text(
        """# Compact Memory - cap_test

## Summary

PreCompact hook trust 验证结论：配置变更后需要重启 Codex 或新进程 resume，真实 /compact 才会加载新的 hook。
""",
        encoding="utf-8",
    )


def _load_chunks(chunks_path: Path) -> List[Dict]:
    chunks = []
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            chunks.append(json.loads(line))
    return chunks


def _build_env(workdir: Path, *, api_key: str = "", base_url: str = "", vector_enabled: bool = True) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "REME_WORKDIR": str(workdir),
            "REME_HOME": str(workdir / "reme-home"),
            "REME_RUNTIME_ENV_FILE": str(workdir / "test-runtime.env"),
            "REME_FTS_ENABLED": "true",
            "REME_VECTOR_ENABLED": "true" if vector_enabled else "false",
            "REME_EMBEDDING_MODEL": "embedding-3",
            "REME_EMBEDDING_DIMENSIONS": "256",
            "REME_EMBEDDING_USE_DIMENSIONS": "true",
            "REME_EMBEDDING_ENABLE_CACHE": "false",
        }
    )

    if api_key:
        env["EMBEDDING_API_KEY"] = api_key
    else:
        env.pop("EMBEDDING_API_KEY", None)

    if base_url:
        env["EMBEDDING_BASE_URL"] = base_url
    else:
        env.pop("EMBEDDING_BASE_URL", None)

    return env


def _parse_json_output(cp: subprocess.CompletedProcess) -> object:
    text = cp.stdout.strip()
    _assert(text, f"stdout is empty. stderr={cp.stderr}")
    return json.loads(text)


def _test_normalization() -> None:
    _assert(
        normalize_embedding_base_url("https://open.bigmodel.cn/api") == "https://open.bigmodel.cn/api/paas/v4",
        "bigmodel /api 规范化失败",
    )
    _assert(
        normalize_embedding_base_url("https://open.bigmodel.cn/api/paas") == "https://open.bigmodel.cn/api/paas/v4",
        "bigmodel /api/paas 规范化失败",
    )
    _assert(
        normalize_embedding_base_url("https://open.bigmodel.cn/api/paas/v4/embeddings")
        == "https://open.bigmodel.cn/api/paas/v4",
        "bigmodel /embeddings 规范化失败",
    )


def _test_fts_fallback(test_root: Path) -> None:
    env = _build_env(test_root, vector_enabled=True)

    rebuild = _run([PYTHON, str(REBUILD_SCRIPT), "--confirm-full-rebuild"], env=env)
    _assert(rebuild.returncode == 0, f"FTS fallback rebuild failed: {rebuild.stderr}")
    rebuild_result = _parse_json_output(rebuild)
    _assert(rebuild_result["vector_enabled"] is False, "无 key 时不应启用向量检索")
    _assert(rebuild_result["fts_enabled"] is True, "FTS 应保持启用")
    _assert(rebuild_result["indexed_files"] == 4, f"应同时索引 memory 和 compact_memory: {rebuild_result}")

    no_hit = _run(
        [
            PYTHON,
            str(SEARCH_SCRIPT),
            "--query",
            "慢启动确认竞争先怎么处理",
            "--max-results",
            "3",
        ],
        env=env,
    )
    _assert(no_hit.returncode == 0, f"FTS fallback search failed: {no_hit.stderr}")
    no_hit_result = _parse_json_output(no_hit)
    _assert(no_hit_result == [], "无向量时，语义改写查询应为空结果")

    keyword_hit = _run(
        [
            PYTHON,
            str(SEARCH_SCRIPT),
            "--query",
            "HostSync",
            "--max-results",
            "3",
        ],
        env=env,
    )
    _assert(keyword_hit.returncode == 0, f"FTS keyword search failed: {keyword_hit.stderr}")
    keyword_result = _parse_json_output(keyword_hit)
    _assert(keyword_result, "FTS 关键词查询应命中样本")

    compact_hit = _run(
        [
            PYTHON,
            str(SEARCH_SCRIPT),
            "--query",
            "PreCompact hook trust",
            "--max-results",
            "3",
        ],
        env=env,
    )
    _assert(compact_hit.returncode == 0, f"compact memory search failed: {compact_hit.stderr}")
    compact_result = _parse_json_output(compact_hit)
    _assert(compact_result, "FTS 查询应命中 compact_memory 样本")
    _assert(
        "compact_memory" in compact_result[0]["path"],
        f"compact memory 查询首条结果不正确: {compact_result}",
    )


def _test_vector_rebuild_and_search(test_root: Path, api_key: str, base_url: str) -> None:
    env = _build_env(test_root, api_key=api_key, base_url=base_url, vector_enabled=True)

    rebuild = _run([PYTHON, str(REBUILD_SCRIPT), "--confirm-full-rebuild"], env=env, timeout=180)
    _assert(rebuild.returncode == 0, f"vector rebuild failed: {rebuild.stderr}")
    rebuild_result = _parse_json_output(rebuild)
    _assert(rebuild_result["vector_enabled"] is True, "有 key 时应启用向量检索")
    _assert(
        rebuild_result["embedding_base_url"] == "https://open.bigmodel.cn/api/paas/v4",
        "BigModel base_url 规范化结果不正确",
    )

    chunks_path = test_root / "file_store" / "reme_local_chunks.jsonl"
    _assert(chunks_path.exists(), "重建后应生成 chunks 索引文件")
    chunks = _load_chunks(chunks_path)
    _assert(chunks, "chunks 索引不能为空")
    first_embedding = chunks[0].get("embedding") or []
    _assert(len(first_embedding) == 256, f"embedding 维度应为 256，实际为 {len(first_embedding)}")
    _assert(any(abs(value) > 1e-12 for value in first_embedding), "真实向量不应全为 0")

    semantic_query = "慢启动确认竞争先怎么处理"
    semantic_hit = _run(
        [
            PYTHON,
            str(SEARCH_SCRIPT),
            "--query",
            semantic_query,
            "--max-results",
            "3",
            "--vector-weight",
            "1.0",
            "--min-score",
            "0.1",
        ],
        env=env,
        timeout=180,
    )
    _assert(semantic_hit.returncode == 0, f"semantic search failed: {semantic_hit.stderr}")
    semantic_result = _parse_json_output(semantic_hit)
    _assert(semantic_result, "启用向量后，语义改写查询应命中结果")
    _assert(
        semantic_result[0]["path"].endswith("2026-04-20.md"),
        f"语义查询首条结果不正确: {semantic_result}",
    )

    provision_hit = _run(
        [
            PYTHON,
            str(SEARCH_SCRIPT),
            "--query",
            "高频通知不要读数据库应该怎么做",
            "--max-results",
            "3",
            "--vector-weight",
            "1.0",
            "--min-score",
            "0.1",
        ],
        env=env,
        timeout=180,
    )
    _assert(provision_hit.returncode == 0, f"Provision semantic search failed: {provision_hit.stderr}")
    provision_result = _parse_json_output(provision_hit)
    _assert(provision_result, "Provision 语义查询应命中结果")
    _assert(
        provision_result[0]["path"].endswith("2026-04-21.md"),
        f"Provision 查询首条结果不正确: {provision_result}",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Test ReMe hybrid vector retrieval against an OpenAI-compatible embedding API.")
    parser.add_argument("--api-key", default="", help="Embedding API key. Optional for fallback-only checks.")
    parser.add_argument(
        "--base-url",
        default="https://open.bigmodel.cn/api",
        help="Embedding base URL. Use the shorter /api form to verify normalization.",
    )
    args = parser.parse_args()

    test_root = Path(tempfile.mkdtemp(prefix="reme_vector_test_", dir="/tmp"))
    try:
        _write_memory_samples(test_root / "memory")
        _write_compact_memory_samples(test_root / "compact_memory")

        results = []

        _test_normalization()
        results.append({"case": "normalize_bigmodel_base_url", "status": "passed"})

        _test_fts_fallback(test_root)
        results.append({"case": "fts_fallback_without_credentials", "status": "passed"})

        if args.api_key:
            _test_vector_rebuild_and_search(test_root, args.api_key, args.base_url)
            results.append({"case": "vector_rebuild_and_semantic_search", "status": "passed"})
        else:
            results.append({"case": "vector_rebuild_and_semantic_search", "status": "skipped", "reason": "no_api_key"})

        print(
            json.dumps(
                {
                    "status": "ok",
                    "test_root": str(test_root),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


if __name__ == "__main__":
    main()
