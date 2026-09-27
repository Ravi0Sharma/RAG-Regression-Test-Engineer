"""Shared configuration, artifacts and resource accounting."""

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def safe_relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise ValueError("Expected a safe relative path")
    return path


def load_env(path=Path(".env")):
    """Load literal KEY=VALUE pairs, never execute shell syntax."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.replace("_", "").isalnum():
            raise ValueError("Invalid .env entry")
        os.environ.setdefault(key, value.strip().strip("\"'"))


@dataclass
class Config:
    model: str = "gpt-6-luna"
    reasoning_effort: str = "high"
    embedding_model: str = "text-embedding-3-small"
    retrieval: str = "hybrid"
    top_k: int = 8
    chunk_lines: int = 100
    max_queries: int = 3
    max_search_rounds: int = 6
    max_test_versions: int = 6
    max_stagnant_steps: int = 2
    max_seconds: int = 600
    test_timeout: int = 120
    max_api_calls: int = 24
    max_tokens: int = 100000
    max_output_tokens: int = 4096
    max_cost_usd: float = 1.0
    input_usd_per_million: float = 0.10
    output_usd_per_million: float = 0.50
    embedding_usd_per_million: float = 0.02
    docker_platform: str = "linux/amd64"
    docker_memory: str = "4g"
    docker_cpus: int = 2

    @classmethod
    def load(cls, path):
        config = cls(**read_json(path))
        config.validate()
        return config

    def validate(self):
        for field in fields(self):
            value = getattr(self, field.name)
            expected = type(field.default)
            if expected in (int, float):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or (expected is int and not isinstance(value, int)):
                    raise ValueError("Invalid numeric type: " + field.name)
            elif not isinstance(value, expected) or not value:
                raise ValueError("Invalid configuration: " + field.name)
            if isinstance(value, (int, float)) and (not math.isfinite(value) or value <= 0):
                raise ValueError("Configuration must be positive: " + field.name)
        if self.retrieval not in {"lexical", "hybrid"}:
            raise ValueError("retrieval must be lexical or hybrid")


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, config):
        self.config = config
        self.started = time.monotonic()
        self.api_calls = self.input_tokens = self.output_tokens = self.embedding_tokens = 0
        self.cost_usd = 0.0

    @property
    def remaining_seconds(self):
        return self.config.max_seconds - (time.monotonic() - self.started)

    def check(self, reserve_tokens=0, reserve_cost=0.0, api=False):
        if self.remaining_seconds <= 0:
            raise BudgetExceeded("time_budget")
        if api and self.api_calls >= self.config.max_api_calls:
            raise BudgetExceeded("api_call_budget")
        if self.input_tokens + self.output_tokens + self.embedding_tokens + reserve_tokens > self.config.max_tokens:
            raise BudgetExceeded("token_budget")
        if self.cost_usd + reserve_cost > self.config.max_cost_usd:
            raise BudgetExceeded("cost_budget")

    def record(self, usage, embedding=False):
        if embedding:
            tokens = usage["total_tokens"]
            self.embedding_tokens += tokens
            self.cost_usd += tokens * self.config.embedding_usd_per_million / 1_000_000
        else:
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]
            self.cost_usd += (usage["input_tokens"] * self.config.input_usd_per_million
                              + usage["output_tokens"] * self.config.output_usd_per_million) / 1_000_000

    def summary(self):
        return {"api_calls": self.api_calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "embedding_tokens": self.embedding_tokens,
                "estimated_cost_usd": round(self.cost_usd, 8),
                "seconds": round(time.monotonic() - self.started, 3)}


class Events:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event, **data):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": time.time(), "event": event, **data}, ensure_ascii=False) + "\n")
