#!/usr/bin/env python3
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme"))
REME_WORKDIR = os.environ.get("REME_WORKDIR", str(Path(REME_HOME) / "light_data"))
RUNTIME_CONFIG_DIR = Path(
    os.environ.get(
        "REME_RUNTIME_CONFIG_DIR",
        str(CODEX_HOME / "memories/reme-memory/daemon/config"),
    )
)
ACTIVE_ENV_FILE = Path(
    os.environ.get(
        "REME_RUNTIME_ENV_FILE",
        str(RUNTIME_CONFIG_DIR / "active.env"),
    )
)
LEGACY_ENV_FILE = Path(REME_HOME) / ".env"


def _load_env_file(path: Path, *, override: bool) -> bool:
    if not path.exists():
        return False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if override or key not in os.environ:
            os.environ[key] = value

    return True


def load_runtime_env(*, override: bool = False) -> Optional[Path]:
    """Load legacy ReMe env first, then override it with the active runtime env."""
    runtime_env_file = Path(os.environ.get("REME_RUNTIME_ENV_FILE", str(ACTIVE_ENV_FILE)))
    reme_home = Path(os.environ.get("REME_HOME", REME_HOME))
    legacy_env_file = reme_home / ".env"
    loaded_path = None
    if _load_env_file(legacy_env_file, override=override):
        loaded_path = legacy_env_file
    if _load_env_file(runtime_env_file, override=True):
        loaded_path = runtime_env_file
    return loaded_path


ACTIVE_RUNTIME_ENV = load_runtime_env(override=False)
REME_MODEL = os.environ.get("REME_REFINE_MODEL", "codex-exec")

DEFAULT_STORE_NAME = os.environ.get("REME_FILE_STORE_NAME", "reme_local")
DEFAULT_CHUNK_TOKENS = int(os.environ.get("REME_CHUNK_TOKENS", "400"))
DEFAULT_CHUNK_OVERLAP = int(os.environ.get("REME_CHUNK_OVERLAP", "80"))
DEFAULT_VECTOR_WEIGHT = float(os.environ.get("REME_VECTOR_WEIGHT", "0.7"))
DEFAULT_MIN_SCORE = float(os.environ.get("REME_MIN_SCORE", "0.1"))
DEFAULT_CANDIDATE_MULTIPLIER = float(os.environ.get("REME_CANDIDATE_MULTIPLIER", "3.0"))
DEFAULT_RERANK_MULTIPLIER = float(os.environ.get("REME_RERANK_MULTIPLIER", "5.0"))
DEFAULT_RECENCY_WEIGHT = float(os.environ.get("REME_RECENCY_WEIGHT", "0.10"))
DEFAULT_RECENCY_HALF_LIFE_DAYS = float(os.environ.get("REME_RECENCY_HALF_LIFE_DAYS", "30"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_embedding_base_url(base_url: str) -> str:
    normalized = base_url.strip()
    if not normalized:
        return normalized

    normalized = normalized.rstrip("/")

    if "open.bigmodel.cn" not in normalized:
        return normalized

    if normalized.endswith("/embeddings"):
        return normalized[: -len("/embeddings")]

    if normalized.endswith("/api"):
        return normalized + "/paas/v4"

    if normalized.endswith("/api/paas"):
        return normalized + "/v4"

    return normalized


def get_llm_config() -> dict:
    runtime_env_file = Path(os.environ.get("REME_RUNTIME_ENV_FILE", str(ACTIVE_ENV_FILE)))
    legacy_env_file = Path(os.environ.get("REME_HOME", REME_HOME)) / ".env"
    env_file = ""
    if runtime_env_file.exists():
        env_file = str(runtime_env_file)
    elif legacy_env_file.exists():
        env_file = str(legacy_env_file)

    return {
        "backend": "codex_exec_refine",
        "model_name": REME_MODEL,
        "base_url": "",
        "api_key_present": False,
        "env_file": env_file,
    }


def get_embedding_config() -> dict:
    base_url = normalize_embedding_base_url(os.environ.get("EMBEDDING_BASE_URL", ""))
    model_name = os.environ.get("REME_EMBEDDING_MODEL", "embedding-3")
    dimensions = int(os.environ.get("REME_EMBEDDING_DIMENSIONS", "2048"))
    use_dimensions = _env_bool("REME_EMBEDDING_USE_DIMENSIONS", True)

    return {
        "backend": "openai",
        "model_name": model_name,
        "dimensions": dimensions,
        "use_dimensions": use_dimensions,
        "api_key": os.environ.get("EMBEDDING_API_KEY", ""),
        "base_url": base_url,
        "enable_cache": _env_bool("REME_EMBEDDING_ENABLE_CACHE", True),
        "max_batch_size": int(os.environ.get("REME_EMBEDDING_MAX_BATCH_SIZE", "10")),
        "max_retries": int(os.environ.get("REME_EMBEDDING_MAX_RETRIES", "3")),
        "max_input_length": int(os.environ.get("REME_EMBEDDING_MAX_INPUT_LENGTH", "8192")),
        "max_cache_size": int(os.environ.get("REME_EMBEDDING_MAX_CACHE_SIZE", "10000")),
    }


def is_vector_runtime_enabled() -> bool:
    requested = _env_bool("REME_VECTOR_ENABLED", True)
    embedding_config = get_embedding_config()
    has_credentials = bool(embedding_config["api_key"] and embedding_config["base_url"])
    return requested and has_credentials


def get_file_store_config() -> dict:
    return {
        "backend": "local",
        "store_name": DEFAULT_STORE_NAME,
        "fts_enabled": _env_bool("REME_FTS_ENABLED", True),
        "vector_enabled": is_vector_runtime_enabled(),
    }


def get_memory_dir() -> Path:
    return Path(os.environ.get("REME_WORKDIR", REME_WORKDIR)) / "memory"


def get_compact_memory_dir() -> Path:
    workdir = Path(os.environ.get("REME_WORKDIR", REME_WORKDIR))
    return Path(os.environ.get("REME_COMPACT_MEMORY_DIR", str(workdir / "compact_memory")))


def get_compact_archive_dir() -> Path:
    workdir = Path(os.environ.get("REME_WORKDIR", REME_WORKDIR))
    return Path(os.environ.get("REME_COMPACT_ARCHIVE_DIR", str(workdir / "compact_archive")))


def get_indexable_memory_dirs() -> List[Path]:
    dirs = [get_memory_dir(), get_compact_memory_dir()]
    seen = set()
    result = []
    for path in dirs:
        key = str(path.expanduser().resolve())
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def get_indexable_memory_files() -> List[Path]:
    files = []
    for directory in get_indexable_memory_dirs():
        if directory.exists():
            files.extend(sorted(directory.glob("*.md")))
    return sorted(files, key=lambda path: str(path.expanduser().resolve()))


def get_file_store_dir() -> Path:
    return Path(os.environ.get("REME_WORKDIR", REME_WORKDIR)) / "file_store"


def get_embedding_cache_dir() -> Path:
    return Path(os.environ.get("REME_WORKDIR", REME_WORKDIR)) / "embedding_cache"


async def init_local_store_with_metrics():
    load_started = time.monotonic()
    loaded_path = load_runtime_env(override=True)
    load_runtime_env_ms = int((time.monotonic() - load_started) * 1000)

    embedding_config = get_embedding_config()
    file_store_config = get_file_store_config()

    embedding_model = None
    embedding_model_import_ms = 0
    embedding_model_construct_ms = 0
    embedding_model_start_ms = 0
    if file_store_config["vector_enabled"]:
        import_started = time.monotonic()
        from reme.core.embedding.openai_embedding_model import OpenAIEmbeddingModel
        embedding_model_import_ms = int((time.monotonic() - import_started) * 1000)
        construct_started = time.monotonic()
        embedding_model = OpenAIEmbeddingModel(
            api_key=embedding_config["api_key"],
            base_url=embedding_config["base_url"],
            model_name=embedding_config["model_name"],
            dimensions=embedding_config["dimensions"],
            use_dimensions=embedding_config["use_dimensions"],
            enable_cache=embedding_config["enable_cache"],
            max_batch_size=embedding_config["max_batch_size"],
            max_retries=embedding_config["max_retries"],
            max_input_length=embedding_config["max_input_length"],
            max_cache_size=embedding_config["max_cache_size"],
            cache_dir=get_embedding_cache_dir(),
            raise_exception=False,
        )
        embedding_model_construct_ms = int((time.monotonic() - construct_started) * 1000)
        embedding_start_started = time.monotonic()
        await embedding_model.start()
        embedding_model_start_ms = int((time.monotonic() - embedding_start_started) * 1000)

    file_store_import_started = time.monotonic()
    from reme.core.file_store.local_file_store import LocalFileStore
    file_store_import_ms = int((time.monotonic() - file_store_import_started) * 1000)
    file_store_construct_started = time.monotonic()
    file_store = LocalFileStore(
        store_name=file_store_config["store_name"],
        db_path=get_file_store_dir(),
        embedding_model=embedding_model,
        vector_enabled=file_store_config["vector_enabled"],
        fts_enabled=file_store_config["fts_enabled"],
    )
    file_store_construct_ms = int((time.monotonic() - file_store_construct_started) * 1000)
    file_store_start_started = time.monotonic()
    await file_store.start()
    file_store_start_ms = int((time.monotonic() - file_store_start_started) * 1000)

    return embedding_model, file_store, {
        "load_runtime_env_ms": load_runtime_env_ms,
        "loaded_env_file": str(loaded_path) if loaded_path is not None else "",
        "embedding_model_import_ms": embedding_model_import_ms,
        "embedding_model_construct_ms": embedding_model_construct_ms,
        "embedding_model_start_ms": embedding_model_start_ms,
        "file_store_import_ms": file_store_import_ms,
        "file_store_construct_ms": file_store_construct_ms,
        "file_store_start_ms": file_store_start_ms,
        "vector_enabled": file_store_config["vector_enabled"],
        "fts_enabled": file_store_config["fts_enabled"],
    }


async def init_local_store():
    embedding_model, file_store, _metrics = await init_local_store_with_metrics()
    return embedding_model, file_store


async def close_local_store_with_metrics(embedding_model, file_store) -> dict:
    close_file_store_ms = 0
    close_embedding_model_ms = 0
    if file_store is not None:
        close_started = time.monotonic()
        await file_store.close()
        close_file_store_ms = int((time.monotonic() - close_started) * 1000)
    if embedding_model is not None:
        close_started = time.monotonic()
        await embedding_model.close()
        close_embedding_model_ms = int((time.monotonic() - close_started) * 1000)
    return {
        "close_file_store_ms": close_file_store_ms,
        "close_embedding_model_ms": close_embedding_model_ms,
    }


async def close_local_store(embedding_model, file_store) -> None:
    await close_local_store_with_metrics(embedding_model, file_store)


def get_runtime_summary() -> Tuple[dict, dict]:
    return get_embedding_config(), get_file_store_config()
