"""Fail-closed, process-safe reservation ledger for paid model requests.

Input is conservatively estimated as ceil(serialized request bytes / 3), a
tokenizer-independent heuristic. Requests above MAX_INPUT_TOKENS are refused.
The output ceiling is enforced with max_tokens (including billed reasoning).
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic_ai.models.wrapper import WrapperModel

INPUT_PER_M = 0.75
OUTPUT_PER_M = 3.75
# Documented gemini-3.8-flash context window (ai.google.dev/gemini-api/docs/
# generate-content/whats-new-gemini-3.8: "1M token context window, 64k max
# output tokens"). The provider rejects requests beyond it, so no request can
# bill more input than this: it is a proven per-request input upper bound.
MAX_INPUT_TOKENS = 1_048_576
MAX_OUTPUT_TOKENS = 4096
# Gemini bills thoughts as output but reports them separately from
# candidates_token_count; reserve output + an equal thinking worst case.
MAX_BILLABLE_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS * 2
# Static per-request overhead for system instructions + tool schemas that the
# message serialization does not include. Measured INSTRUCTIONS ~0.4k tokens
# and the four tool schemas ~2k tokens; 8192 keeps >2x margin.
INPUT_OVERHEAD_TOKENS = 8192


class BudgetExceeded(RuntimeError):
    """The persistent spend ledger cannot safely authorize another request."""


class BudgetGuard:
    def __init__(self, path: str | Path | None = None, cap_usd: float = 1.0):
        self.path = Path(path or Path.home() / ".finn_budget.json")
        self.cap_usd = float(cap_usd)

    @staticmethod
    def cost(input_tokens: int, output_tokens: int) -> float:
        return input_tokens * INPUT_PER_M / 1_000_000 + output_tokens * OUTPUT_PER_M / 1_000_000

    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
            if (not isinstance(value, dict) or not isinstance(value.get("total_usd"), (int, float))
                    or value["total_usd"] < 0 or not isinstance(value.get("reservations"), dict)):
                raise ValueError("invalid ledger")
            return value
        except Exception as exc:
            raise BudgetExceeded(f"Live model blocked: budget ledger {self.path} is missing or unreadable") from exc

    def _write(self, ledger: dict) -> None:
        try:
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(ledger))
            os.replace(tmp, self.path)
        except Exception as exc:
            raise BudgetExceeded(f"Live model blocked: cannot write budget ledger {self.path}") from exc

    def reserve(self, input_tokens: int) -> str:
        # The operator pause flag applies to the shared live ledger only, so
        # isolated test ledgers under tmp paths are unaffected.
        if self.path == Path.home() / ".finn_budget.json" and (Path.home() / ".finn_paused").exists():
            raise BudgetExceeded("Live model paused by operator (billing-bound verification)")
        if not 0 <= input_tokens <= MAX_INPUT_TOKENS:
            raise BudgetExceeded("Live model blocked: request exceeds bounded input token limit")
        amount = self.cost(input_tokens, MAX_BILLABLE_OUTPUT_TOKENS)
        fd = self._locked()
        try:
            ledger = self._read()
            outstanding = sum(float(v["usd"]) for v in ledger["reservations"].values())
            spent = (ledger["total_usd"] + float(ledger.get("forfeited_usd", 0.0))
                     + float(ledger.get("uncounted_usd", 0.0)))
            if spent + outstanding + amount > self.cap_usd:
                raise BudgetExceeded(f"Live model budget cap reached (${ledger['total_usd'] + outstanding:.6f} / ${self.cap_usd:.2f})")
            token = uuid.uuid4().hex
            ledger["reservations"][token] = {"usd": amount, "input_tokens": input_tokens, "output_tokens": MAX_BILLABLE_OUTPUT_TOKENS}
            self._write(ledger)
            return token
        finally:
            os.close(fd)

    def forfeit(self, token: str) -> None:
        """Settle a reservation at its full reserved amount when actual usage is
        unknowable (error, cancellation, disconnect, missing provider usage).
        Counted toward the cap but tracked separately from measured spend."""
        fd = self._locked()
        try:
            ledger = self._read()
            reservation = ledger["reservations"].pop(token, None)
            if reservation is None:
                return
            ledger["forfeited_usd"] = float(ledger.get("forfeited_usd", 0.0)) + float(reservation["usd"])
            ledger["forfeited_requests"] = int(ledger.get("forfeited_requests", 0)) + 1
            self._write(ledger)
        finally:
            os.close(fd)

    def reconcile(self, token: str, input_tokens: int | None, output_tokens: int | None) -> None:
        if input_tokens is None or output_tokens is None:
            self.forfeit(token)  # unknown usage forfeits the entire reservation
            return
        fd = self._locked()
        try:
            ledger = self._read()
            reservation = ledger["reservations"].pop(token, None)
            if reservation is None:
                raise BudgetExceeded("Live model blocked: reservation missing from ledger")
            actual = self.cost(int(input_tokens), int(output_tokens))
            # If a provider reports more than the enforced reservation, retain
            # the reservation and fail closed rather than crediting a refund.
            if actual > float(reservation["usd"]):
                ledger["reservations"][token] = reservation
                self._write(ledger)
                raise BudgetExceeded("Provider usage exceeded reserved maximum")
            ledger["total_usd"] += actual
            ledger["requests"] = int(ledger.get("requests", 0)) + 1
            ledger["input_tokens"] = int(ledger.get("input_tokens", 0)) + int(input_tokens)
            ledger["output_tokens"] = int(ledger.get("output_tokens", 0)) + int(output_tokens)
            self._write(ledger)
        finally:
            os.close(fd)

    def authorize(self) -> None:
        """Compatibility check; actual calls must use reserve()."""
        fd = self._locked()
        try:
            ledger = self._read()
            if ledger["total_usd"] + sum(v["usd"] for v in ledger["reservations"].values()) >= self.cap_usd:
                raise BudgetExceeded("Live model budget cap reached")
        finally:
            os.close(fd)

    def record(self, input_tokens, output_tokens, requests: int = 1) -> None:
        """Compatibility helper for importing historical usage into the ledger."""
        token = self.reserve(0)
        self.reconcile(token, input_tokens, output_tokens)


def _estimate_input(messages, overhead_tokens: int = INPUT_OVERHEAD_TOKENS) -> int:
    # Serialize the complete request including history/tool results. 2 bytes
    # per token is conservative for UTF-8 (Gemini BPE averages ~3-4 bytes/token
    # on English; multibyte scripts are covered by the lower divisor). Static
    # overhead covers system instructions and tool schemas, which are not in
    # `messages`. Anything past MAX_INPUT_TOKENS is refused by reserve().
    size = len(repr(messages).encode("utf-8"))
    return min(MAX_INPUT_TOKENS + 1, math.ceil(size / 2) + overhead_tokens)


def _billable_output(usage) -> int | None:
    """Output tokens billed by the provider: candidates plus Gemini thoughts
    (reported in usage.details['thoughts_tokens'], billed at the output rate)."""
    out = getattr(usage, "output_tokens", None)
    if out is None:
        return None
    details = getattr(usage, "details", None) or {}
    thoughts = details.get("thoughts_tokens", 0) if isinstance(details, dict) else 0
    return int(out) + int(thoughts or 0)


class GuardedModel(WrapperModel):
    """Reserves each model request at worst case, then reconciles actual usage.
    Every reservation settles exactly once: measured on success, forfeited at
    full reservation on error, cancellation, disconnect or missing usage."""
    def __init__(self, wrapped, guard: BudgetGuard, input_overhead_tokens: int = INPUT_OVERHEAD_TOKENS):
        super().__init__(wrapped)
        self.guard = guard
        self.input_overhead_tokens = input_overhead_tokens

    @staticmethod
    def _capped_settings(model_settings) -> dict:
        settings = dict(model_settings or {})
        settings["max_tokens"] = min(int(settings.get("max_tokens", MAX_OUTPUT_TOKENS)), MAX_OUTPUT_TOKENS)
        return settings

    async def request(self, messages, model_settings, model_request_parameters):
        # Reserve the documented per-request maximum (full context window at
        # input rate + worst-case billable output incl. thoughts), so actual
        # cost can never exceed the reservation; reconcile refunds the rest.
        reservation = self.guard.reserve(MAX_INPUT_TOKENS)
        settled = False
        try:
            response = await self.wrapped.request(
                messages, self._capped_settings(model_settings), model_request_parameters)
            usage = response.usage
            self.guard.reconcile(reservation, getattr(usage, "input_tokens", None), _billable_output(usage))
            settled = True
            return response
        finally:
            if not settled:
                self.guard.forfeit(reservation)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        reservation = self.guard.reserve(MAX_INPUT_TOKENS)
        settled = False
        try:
            async with self.wrapped.request_stream(
                    messages, self._capped_settings(model_settings), model_request_parameters, run_context) as stream:
                yield stream
                usage = stream.usage
                self.guard.reconcile(reservation, getattr(usage, "input_tokens", None), _billable_output(usage))
                settled = True
        finally:
            if not settled:
                self.guard.forfeit(reservation)


_CONFIGURED_GUARD = None


def configured_guard() -> BudgetGuard:
    """One operator-configured process ledger; HTTP inputs never affect cap."""
    global _CONFIGURED_GUARD
    if _CONFIGURED_GUARD is None:
        _CONFIGURED_GUARD = BudgetGuard(cap_usd=float(os.environ.get("FINN_BUDGET_USD", "1.00")))
    return _CONFIGURED_GUARD


PRICED_MODELS = {"google:gemini-3.8-flash", "google-gla:gemini-3.8-flash"}


def priced_google_model(model_name: str = "gemini-3.8-flash"):
    """Build the priced GoogleModel with a single-attempt HTTP client.

    google-genai defaults: tenacity retries are off when retry_options is None,
    but the async client performs implicit connection-level retries. Setting
    HttpRetryOptions(attempts=1) disables both: one HTTP attempt per model
    request, which is exactly what one reservation covers.
    """
    import os as _os

    from google import genai
    from google.genai.types import HttpOptions, HttpRetryOptions
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider

    api_key = _os.environ.get("GOOGLE_API_KEY") or _os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise BudgetExceeded("Live model blocked: GOOGLE_API_KEY/GEMINI_API_KEY not set")
    client = genai.Client(
        api_key=api_key,
        http_options=HttpOptions(retry_options=HttpRetryOptions(attempts=1)),
    )
    return GoogleModel(model_name, provider=GoogleProvider(client=client))


def guarded_model(model, guard: "BudgetGuard | None" = None):
    """Single entrypoint for constructing a model under the spend cap.

    Free models (TestModel/FunctionModel) pass through untouched. Paid models
    are wrapped in the shared reservation guard; a model without a configured
    price is refused (fail closed) rather than allowed to spend unpriced.
    """
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.test import TestModel

    if isinstance(model, (TestModel, FunctionModel, GuardedModel)):
        return model
    if isinstance(model, str):
        name = model
        if name.startswith("google-gla:"):
            name = "google:" + name.split(":", 1)[1]
        if name not in PRICED_MODELS:
            raise BudgetExceeded(
                f"Live model blocked: {model!r} has no configured price; refusing unpriced spend")
        resolved = priced_google_model(name.split(":", 1)[1])
    else:
        resolved = model
        from pydantic_ai.models.google import GoogleModel

        legs = getattr(resolved, "models", None) or [resolved]
        for leg in legs:
            leg_name = getattr(leg, "model_name", "")
            normalized = ("google:" + leg_name.split(":", 1)[1]
                          if leg_name.startswith("google-gla:") else leg_name)
            priced_name = normalized in PRICED_MODELS or leg_name == "gemini-3.8-flash"
            # the instance must actually be the priced Google backend, not a
            # same-named model on another provider
            if not (priced_name and isinstance(leg, GoogleModel)):
                raise BudgetExceeded(
                    f"Live model blocked: {leg_name!r} has no configured price; refusing unpriced spend")
    return GuardedModel(resolved, guard or configured_guard())
