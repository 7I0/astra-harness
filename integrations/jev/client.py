from __future__ import annotations

"""Small standard-library Jev client with explicit live/stub provenance."""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

JEV_URL = "https://api.typesafe.ai/v1/systemone"
PINNED_MODEL = "jev-1.13.0"
STUB_MODEL = "stub-rules-v0"
NONE_OF_THESE = "none_of_these"

_SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I | re.S)),
    ("authorization", re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic\s+))[^\s,;]+")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("cookie", re.compile(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("secret_assignment", re.compile(r"(?i)\b(api[_-]?key|secret|token|password|private[_-]?key)\s*[:=]\s*[\"']?[^\s,;\"']{8,}")),
    ("hex_private", re.compile(r"(?i)(?<![0-9a-f])0x[0-9a-f]{64}(?![0-9a-f])")),
    ("mnemonic", re.compile(r"(?i)\b(?:seed phrase|mnemonic)\s*[:=]\s*(?:[a-z]+\s+){11,23}[a-z]+\b")),
)


class JevAuthenticationError(RuntimeError):
    """A definitive vendor credential rejection; callers must stop."""


@dataclass(frozen=True)
class JevResult:
    request_id: str
    model_requested: str
    model_returned: str
    backend: str
    answers: dict[str, Any]
    input_tokens: int
    error: str | None = None
    truncated: bool = False
    latency_ms: float | None = None
    attempt_count: int = 0
    http_status: int | None = None
    backend_reason: str | None = None
    requested_at: str | None = None
    received_at: str | None = None


def redact_sensitive(value: Any) -> Any:
    """Redact credential-shaped material before a request is sent or retained."""
    if isinstance(value, dict):
        return {str(key): redact_sensitive(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(child) for child in value]
    if not isinstance(value, str):
        return value
    output = value
    for kind, pattern in _SECRET_PATTERNS:
        if kind == "authorization":
            output = pattern.sub(r"\1[REDACTED]", output)
        else:
            output = pattern.sub(f"[REDACTED:{kind}]", output)
    return output


def state_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stub_answers(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
    """Deterministic neutral-first fallback; never represented as live Jev."""
    facts = state if isinstance(state, dict) else {}
    guessed = facts.get("mechanism_guess") or NONE_OF_THESE
    output: dict[str, Any] = {}
    for key, question in questions.items():
        options = list((question.get("criteria") or {}).keys())
        choice = guessed if guessed in options else NONE_OF_THESE
        probabilities = {option: 0.0 for option in options}
        if choice in probabilities:
            probabilities[choice] = 0.7
            remainder = [option for option in options if option != choice]
            for option in remainder:
                probabilities[option] = round(0.3 / len(remainder), 3) if remainder else 0.0
        output[key] = {
            "type": "choice",
            "choice": choice,
            "confidence": 0.5,
            "probabilities": probabilities,
        }
    return output


class JevClient:
    """Call Jev when explicitly configured, otherwise return labelled stub output."""

    def __init__(
        self,
        *,
        model: str = PINNED_MODEL,
        api_key: str | None = None,
        force_stub: bool = False,
        timeout: float = 30.0,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.api_key = None if force_stub else (api_key or os.environ.get("TYPESAFE_API_KEY"))
        self.force_stub = force_stub
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep

    def ask(self, state: Any, questions: dict[str, Any], *, item_id: str) -> JevResult:
        del item_id  # item identity belongs to the caller's evidence ledger.
        safe_state = redact_sensitive(state)
        requested_at = _now()
        started = time.monotonic()
        request_id = f"jev-{state_digest({'time': requested_at, 'state': safe_state})}"
        if self.force_stub or not self.api_key:
            return self._result(
                request_id=request_id,
                model_requested=self.model,
                model_returned=STUB_MODEL,
                backend="stub",
                answers=_stub_answers(safe_state, questions),
                input_tokens=len(json.dumps(safe_state, default=str)) // 4,
                backend_reason="forced_stub" if self.force_stub else "no_credential",
                requested_at=requested_at,
                started=started,
            )

        body = json.dumps({"model": self.model, "state": safe_state, "questions": questions}).encode()
        request = urllib.request.Request(
            JEV_URL,
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        last_error: str | None = None
        status: int | None = None
        for attempt in range(1, 4):
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    status = getattr(response, "status", None)
                    data = json.loads(response.read().decode())
                return self._result(
                    request_id=request_id,
                    model_requested=self.model,
                    model_returned=data.get("model") or self.model,
                    backend="live",
                    answers=data.get("answers") or {},
                    input_tokens=int((data.get("usage") or {}).get("input_tokens") or 0),
                    truncated=bool(data.get("truncated", False)),
                    backend_reason="live_success",
                    requested_at=requested_at,
                    started=started,
                    attempt_count=attempt,
                    http_status=status,
                )
            except urllib.error.HTTPError as exc:
                status = exc.code
                last_error = f"HTTP {exc.code}: vendor request failed"
                if exc.code in (401, 403):
                    raise JevAuthenticationError(last_error) from exc
                if exc.code not in (429, 529):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:300]
            if attempt < 3:
                self._sleep(float(attempt))

        return self._result(
            request_id=request_id,
            model_requested=self.model,
            model_returned=STUB_MODEL,
            backend="stub",
            answers=_stub_answers(safe_state, questions),
            input_tokens=len(json.dumps(safe_state, default=str)) // 4,
            error=last_error,
            backend_reason="transient_failure_stub",
            requested_at=requested_at,
            started=started,
            attempt_count=3,
            http_status=status,
        )

    @staticmethod
    def _result(*, started: float, requested_at: str, **kwargs: Any) -> JevResult:
        return JevResult(
            model_requested=kwargs.pop("model_requested", PINNED_MODEL),
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            received_at=_now(),
            **kwargs,
        )
