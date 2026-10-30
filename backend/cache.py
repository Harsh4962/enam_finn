"""Content-addressed SQLite cache and append-only operation telemetry.

One connection per manager, serialized by an RLock; WAL and a busy timeout allow
multiple managers/processes on the same local disk. Never share a manager across
fork(). This is synchronous I/O: use a worker thread from an async web handler.
JSON and raw bytes only, never pickle. LLM calls are intentionally outside this
module: providers, extraction and orchestration belong to subsequent steps.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Generic, Iterator, Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from .config import (
    CACHE_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TASK_TIERS,
    CacheSettings,
    ModelRoute,
    Task,
    Tier,
    TokenPricing,
    normalize_ticker,
)
from .schemas import EngineTelemetry, TelemetryEvent

T = TypeVar("T", bound=BaseModel)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_NANO_USD = Decimal(1_000_000_000)


class _DefaultTTL(Enum):
    POLICY = "policy"


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json", round_trip=True))
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("cache JSON must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("cache JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported cache JSON value: {type(value).__name__}")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")


def _encode(value: Any) -> tuple[str, bytes]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "bytes", bytes(value)
    return "json", _json_bytes(value)


def content_hash(payload: Any, *, context: Mapping[str, Any] | None = None) -> str:
    """Stable, type-framed SHA-256. JSON key order is irrelevant; list order is not.

    Context is part of the fingerprint. Include source URL/parameters, parser
    version and data scope for scrapes, and the COMPLETE rendered request for
    LLM work. A hash is content identity, not a freshness check.
    """
    encoding, body = _encode(payload)
    digest = hashlib.sha256(b"finn-cache-v1\x00")
    for part in (encoding.encode("ascii"), body, _json_bytes(context or {})):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class CacheKey:
    ticker: str
    data_type: str
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))
        if not isinstance(self.data_type, str) or not _IDENTIFIER.fullmatch(self.data_type):
            raise ValueError("data_type must be a stable identifier, max 128 characters")
        if not isinstance(self.content_hash, str) or not _SHA256.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")

    @property
    def sql_parameters(self) -> tuple[str, str, str]:
        return self.ticker, self.data_type, self.content_hash

    @classmethod
    def for_content(cls, ticker: str, data_type: str, payload: Any, *, context: Mapping[str, Any] | None = None) -> CacheKey:
        return cls(ticker, data_type, content_hash(payload, context=context))

    @classmethod
    def for_llm(
        cls, ticker: str, task: Task | str, inputs: Any, *, route: ModelRoute,
        prompt_version: str, output_model: type[BaseModel], schema_version: str = SCHEMA_VERSION,
    ) -> CacheKey:
        task = Task(task)
        if TASK_TIERS[task] == 0:
            raise ValueError("Tier 0 work must not use an LLM cache key")
        if not prompt_version.strip() or not schema_version.strip():
            raise ValueError("prompt and schema versions must not be blank")
        return cls.for_content(ticker, f"llm.{task.value}", inputs, context={
            "task": task.value, "tier": TASK_TIERS[task], "provider": route.provider,
            "model": route.model, "temperature": route.temperature,
            "max_output_tokens": route.max_output_tokens,
            "prompt_version": prompt_version, "schema_version": schema_version,
            "output_schema": output_model.model_json_schema(mode="validation"),
        })


@dataclass(frozen=True, slots=True)
class CacheLookup(Generic[T]):
    hit: bool
    value: Any = None  # raw JSON/bytes or T; hit distinguishes a cached JSON null
    miss_reason: str | None = None
    created_at: float | None = None  # UTC Unix seconds
    expires_at: float | None = None
    metadata: Mapping[str, Any] | None = None


def _checked_tokens(input_tokens: int, output_tokens: int, cached_input_tokens: int) -> None:
    if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (input_tokens, output_tokens, cached_input_tokens)):
        raise ValueError("token counts must be nonnegative SQLite-sized integers")
    if cached_input_tokens > input_tokens:
        raise ValueError("cached input tokens are a subset of total input tokens")


def estimate_token_cost(
    pricing: TokenPricing | None, *, input_tokens: int, output_tokens: int,
    cached_input_tokens: int = 0,
) -> float | None:
    """Discount cached provider input once; local cache hits use no tokens at all."""
    _checked_tokens(input_tokens, output_tokens, cached_input_tokens)
    if pricing is None:
        return None
    if cached_input_tokens and pricing.cached_input_per_million_usd is None:
        return None
    cached_rate = pricing.cached_input_per_million_usd or 0.0
    cost = (
        Decimal(input_tokens - cached_input_tokens) * Decimal(str(pricing.input_per_million_usd))
        + Decimal(cached_input_tokens) * Decimal(str(cached_rate))
        + Decimal(output_tokens) * Decimal(str(pricing.output_per_million_usd))
    ) / Decimal(1_000_000)
    return float(cost)


@dataclass(slots=True)
class OperationUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    usage_reported: bool = False

    def set_tokens(self, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> None:
        _checked_tokens(input_tokens, output_tokens, cached_input_tokens)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_input_tokens = cached_input_tokens
        self.usage_reported = True


_DDL = (
    """CREATE TABLE IF NOT EXISTS cache_entries (
        ticker TEXT NOT NULL, data_type TEXT NOT NULL, content_hash TEXT NOT NULL,
        encoding TEXT NOT NULL CHECK (encoding IN ('json','bytes')),
        payload BLOB NOT NULL, payload_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL,
        hit_count INTEGER NOT NULL DEFAULT 0, last_accessed_at REAL,
        PRIMARY KEY (ticker, data_type, content_hash)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS cache_expiry_idx ON cache_entries(expires_at) WHERE expires_at IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS telemetry_events (
        event_id TEXT PRIMARY KEY, occurred_at REAL NOT NULL, request_id TEXT, ticker TEXT,
        operation TEXT NOT NULL, tier INTEGER, provider TEXT, model TEXT,
        provider_called INTEGER NOT NULL, cache_status TEXT NOT NULL,
        latency_ms REAL NOT NULL, input_tokens INTEGER NOT NULL,
        cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        cost_nano_usd INTEGER, pricing_source TEXT, status TEXT NOT NULL, error_type TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS telemetry_time_idx ON telemetry_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS telemetry_ticker_time_idx ON telemetry_events(ticker, occurred_at)",
    "CREATE INDEX IF NOT EXISTS telemetry_request_idx ON telemetry_events(request_id)",
)


class SQLiteCache:
    def __init__(
        self, path: str | Path | None = None, *, settings: CacheSettings | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or CacheSettings()
        self.path = str(path if path is not None else self.settings.path)
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser().resolve())
        self._connection = sqlite3.connect(
            self.path, timeout=self.settings.busy_timeout_ms / 1000,
            isolation_level=None, check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute(f"PRAGMA busy_timeout={self.settings.busy_timeout_ms}")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            with self._transaction() as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, CACHE_SCHEMA_VERSION):
                    raise RuntimeError(f"unsupported FINN cache schema version {version}")
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={CACHE_SCHEMA_VERSION}")
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def __enter__(self) -> SQLiteCache:
        if self._closed:
            raise RuntimeError("cache is closed")
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError("cache is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _expiry(self, ttl_seconds: float | None | _DefaultTTL, key: CacheKey, now: float) -> float | None:
        if ttl_seconds is _DefaultTTL.POLICY:
            ttl_seconds = self.settings.llm_ttl_seconds if key.data_type.startswith("llm.") else self.settings.scrape_ttl_seconds
        if ttl_seconds is None:
            return None  # immutable content, explicitly requested
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive, or None for no expiry")
        return now + ttl_seconds

    def put(
        self, key: CacheKey, value: Any, *,
        ttl_seconds: float | None | _DefaultTTL = _DefaultTTL.POLICY,
        metadata: Mapping[str, Any] | None = None, event: TelemetryEvent | None = None,
    ) -> None:
        """Atomic upsert, optionally committing a successful call's ledger event.

        Omit TTL for policy defaults (scrapes 6h, LLM 30d). ``None`` explicitly
        means no expiry. Do not use this to store failed/invalid LLM responses.
        Passing a Pydantic model revalidates it, including nested instances.
        """
        if isinstance(value, BaseModel):
            value = type(value).model_validate_json(_json_bytes(value))
        encoding, body = _encode(value)
        if len(body) > self.settings.max_payload_bytes:
            raise ValueError("cache payload exceeds max_payload_bytes")
        metadata_json = _json_bytes(metadata or {}).decode("utf-8")
        if len(metadata_json.encode("utf-8")) > 64 * 1024:
            raise ValueError("cache metadata exceeds 64 KiB")
        now = self._clock()
        expires_at = self._expiry(ttl_seconds, key, now)
        if event is not None:
            event = TelemetryEvent.model_validate(event)
            if event.status != "success" or event.ticker != key.ticker:
                raise ValueError("atomic cache event must be successful and match the key's ticker")
        with self._transaction() as connection:
            connection.execute("""INSERT INTO cache_entries
                (ticker,data_type,content_hash,encoding,payload,payload_sha256,metadata_json,created_at,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker,data_type,content_hash) DO UPDATE SET
                encoding=excluded.encoding,payload=excluded.payload,payload_sha256=excluded.payload_sha256,
                metadata_json=excluded.metadata_json,created_at=excluded.created_at,expires_at=excluded.expires_at,
                hit_count=0,last_accessed_at=NULL""",
                (*key.sql_parameters, encoding, body, hashlib.sha256(body).hexdigest(), metadata_json, now, expires_at))
            if event is not None:
                self._insert_event(connection, event)

    def get(self, key: CacheKey, *, model: type[T] | None = None, request_id: str | None = None) -> CacheLookup[T]:
        """Read, verify checksum/schema and count exactly one cache lookup.

        Expired, corrupted and schema-incompatible entries are deleted and
        returned as misses. Database/locking errors propagate; they are not
        silently mislabeled as cache misses. Cached JSON null is a real hit.
        """
        started = time.perf_counter()
        now = self._clock()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM cache_entries WHERE ticker=? AND data_type=? AND content_hash=?", key.sql_parameters).fetchone()
            result: CacheLookup[T] = CacheLookup(hit=False, miss_reason="not_found")
            if row is not None:
                reason = None
                value = None
                metadata = None
                if row["expires_at"] is not None and row["expires_at"] <= now:
                    reason = "expired"
                elif hashlib.sha256(row["payload"]).hexdigest() != row["payload_sha256"]:
                    reason = "checksum_mismatch"
                else:
                    try:
                        metadata = _json_value(json.loads(row["metadata_json"]))
                        if not isinstance(metadata, dict):
                            raise ValueError("cache metadata must be an object")
                        if model is not None:
                            if row["encoding"] != "json":
                                raise ValueError("model cache entry must be JSON")
                            value = model.model_validate_json(row["payload"])
                        else:
                            value = _json_value(json.loads(row["payload"])) if row["encoding"] == "json" else row["payload"]
                    except (ValueError, TypeError, UnicodeError, ValidationError):
                        reason = "invalid_payload"
                if reason is not None:
                    connection.execute("DELETE FROM cache_entries WHERE ticker=? AND data_type=? AND content_hash=?", key.sql_parameters)
                    result = CacheLookup(hit=False, miss_reason=reason)
                else:
                    connection.execute("""UPDATE cache_entries SET hit_count=hit_count+1,last_accessed_at=?
                        WHERE ticker=? AND data_type=? AND content_hash=?""", (now, *key.sql_parameters))
                    result = CacheLookup(hit=True, value=value, created_at=row["created_at"], expires_at=row["expires_at"], metadata=metadata)
            self._insert_event(connection, TelemetryEvent(
                ticker=key.ticker, operation=key.data_type, request_id=request_id,
                cache_status="hit" if result.hit else "miss", latency_ms=(time.perf_counter() - started) * 1000,
            ))
            return result

    def delete(self, key: CacheKey) -> bool:
        with self._transaction() as connection:
            return connection.execute("DELETE FROM cache_entries WHERE ticker=? AND data_type=? AND content_hash=?", key.sql_parameters).rowcount == 1

    def invalidate(self, *, ticker: str, data_type: str | None = None) -> int:
        ticker = normalize_ticker(ticker)
        sql = "DELETE FROM cache_entries WHERE ticker=?"
        parameters: list[Any] = [ticker]
        if data_type is not None:
            sql += " AND data_type=?"
            parameters.append(data_type)
        with self._transaction() as connection:
            return connection.execute(sql, parameters).rowcount

    def purge_expired(self) -> int:
        with self._transaction() as connection:
            return connection.execute("DELETE FROM cache_entries WHERE expires_at IS NOT NULL AND expires_at<=?", (self._clock(),)).rowcount

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: TelemetryEvent) -> bool:
        cost = None
        if event.estimated_cost_usd is not None:
            scaled_cost = Decimal(str(event.estimated_cost_usd)) * _NANO_USD
            if scaled_cost > Decimal(2**63 - 1):
                raise ValueError("operation cost exceeds SQLite's ledger range")
            cost = int(scaled_cost.quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return connection.execute("""INSERT INTO telemetry_events
            (event_id,occurred_at,request_id,ticker,operation,tier,provider,model,provider_called,
             cache_status,latency_ms,input_tokens,cached_input_tokens,output_tokens,cost_nano_usd,
             pricing_source,status,error_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO NOTHING""", (
                event.event_id, event.occurred_at.timestamp(), event.request_id, event.ticker,
                event.operation, event.tier, event.provider, event.model, int(event.provider_called),
                event.cache_status, event.latency_ms, event.input_tokens, event.cached_input_tokens,
                event.output_tokens, cost, event.pricing_source, event.status, event.error_type,
            )).rowcount == 1

    def record_event(self, event: TelemetryEvent) -> bool:
        """Append once by event_id. Replaying an event does not double-count cost."""
        event = TelemetryEvent.model_validate(event)
        with self._transaction() as connection:
            return self._insert_event(connection, event)

    @contextmanager
    def track(
        self, operation: str, *, ticker: str | None = None, tier: Tier | None = None,
        route: ModelRoute | None = None, request_id: str | None = None,
    ) -> Iterator[OperationUsage]:
        """Measure ONE actual operation/LLM attempt; call only after a cache miss.

        Call ``usage.set_tokens`` with normalized provider usage, even on a
        failed response when usage is available. Missing usage/prices are
        tracked as unpriced, not zero. Never wrap the whole retry loop: each
        attempt may be billed. Telemetry does not store exception messages.
        """
        if tier not in (None, 0, 1, 2) or isinstance(tier, bool):
            raise ValueError("tier must be None, 0, 1 or 2")
        if (tier in (1, 2)) != (route is not None):
            raise ValueError("only tier 1/2 operations require a model route")
        # Validate boundary metadata before executing user/provider work.
        TelemetryEvent(operation=operation, ticker=ticker, request_id=request_id, latency_ms=0.0)
        if route is not None:
            route.ensure_usable()
        usage = OperationUsage()
        started = time.perf_counter()
        error: BaseException | None = None
        try:
            yield usage
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                _checked_tokens(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)
                cost = 0.0
                if route is not None:
                    cost = estimate_token_cost(route.pricing, input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens, cached_input_tokens=usage.cached_input_tokens) if usage.usage_reported else None
                event = TelemetryEvent(
                    operation=operation, ticker=ticker, tier=tier, request_id=request_id,
                    provider=route.provider if route else None, model=route.model if route else None,
                    provider_called=route is not None, latency_ms=(time.perf_counter() - started) * 1000,
                    input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                    cached_input_tokens=usage.cached_input_tokens, estimated_cost_usd=cost,
                    pricing_source=route.pricing.source_url if route and route.pricing and cost is not None else None,
                    status="error" if error is not None else "success",
                    error_type=type(error).__name__ if error is not None else None,
                )
                self.record_event(event)
            except Exception as ledger_error:
                if error is None:
                    raise
                error.add_note(f"FINN telemetry write also failed: {type(ledger_error).__name__}")

    def telemetry(
        self, *, ticker: str | None = None, since: datetime | None = None,
        until: datetime | None = None, request_id: str | None = None,
    ) -> EngineTelemetry:
        """Costs sum only priced attempts; unpriced_llm_calls flags incompleteness.

        Latency is summed operation time, not end-user wall time when operations
        overlap. Filters use an inclusive start and exclusive end, in UTC.
        """
        clauses: list[str] = []
        parameters: list[Any] = []
        for name, value in (("since", since), ("until", until)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if since is not None and until is not None and since >= until:
            raise ValueError("since must precede until")
        if ticker is not None:
            clauses.append("ticker=?")
            parameters.append(normalize_ticker(ticker))
        if since is not None:
            clauses.append("occurred_at>=?")
            parameters.append(since.timestamp())
        if until is not None:
            clauses.append("occurred_at<?")
            parameters.append(until.timestamp())
        if request_id is not None:
            clauses.append("request_id=?")
            parameters.append(request_id)
        sql = """SELECT COUNT(*) AS events,
            COALESCE(SUM(tier=0),0) AS tier0_calls,
            COALESCE(SUM(tier=1 AND provider_called=1),0) AS tier1_calls,
            COALESCE(SUM(tier=2 AND provider_called=1),0) AS tier2_calls,
            COALESCE(SUM(cache_status='hit'),0) AS cache_hits,
            COALESCE(SUM(cache_status='miss'),0) AS cache_misses,
            COALESCE(SUM(status='error'),0) AS failures,
            COALESCE(SUM(input_tokens),0) AS input_tokens,
            COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
            COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(cost_nano_usd),0) AS cost_nano_usd,
            COALESCE(SUM(provider_called=1 AND cost_nano_usd IS NULL),0) AS unpriced_llm_calls,
            COALESCE(SUM(latency_ms),0.0) AS total_latency_ms,
            COALESCE(MAX(latency_ms),0.0) AS max_latency_ms FROM telemetry_events"""
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            if self._closed:
                raise RuntimeError("cache is closed")
            values = dict(self._connection.execute(sql, parameters).fetchone())
        values["estimated_cost_usd"] = values.pop("cost_nano_usd") / 1_000_000_000
        return EngineTelemetry(since=since, until=until, **values)

    def prune_telemetry(self, *, before: datetime) -> int:
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must be timezone-aware")
        with self._transaction() as connection:
            return connection.execute("DELETE FROM telemetry_events WHERE occurred_at<?", (before.timestamp(),)).rowcount


FinnCache = SQLiteCache
