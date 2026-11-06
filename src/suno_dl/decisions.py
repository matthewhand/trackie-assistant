"""SystemOne decision-client abstraction layer.

Why this module exists
----------------------
"SystemOne" decision models (fast, non-autoregressive judgment models that
answer *typed questions* instead of generating text) converged on one request
shape — ``{"state": ..., "questions": {...}}`` — but several providers serve
it with slightly different endpoints, auth, and answer spellings. Verified
2026-09-28, except the Needle row, which comes from that service's published
API schema:

========================  =======================================  ============
Provider                  Endpoint                                 Answer shape
========================  =======================================  ============
TypeSafe Jev (hosted)     ``POST /v1/systemone`` (Bearer key)      ``label``
Venice.ai                 same handler (``/systemone``)            ``label``
Cloudflare Workers AI     Jev-compatible ``/ai/run``               ``label``
Zait Labs (Laya-backed)   ``laya.inference.zaitlabs.com/ai/run``   ``label``/``noul``
Laya (self-hosted)        ``POST /v1/decisions``                   ``choice`` + ``probabilities``
Local JEV fleet           ``POST /v1/evaluate``                    ``label``/``probabilities``
Needle (hass-needle-app)  ``POST /v1/decisions``                   ``label`` + ``raw_confidence``
========================  =======================================  ============

The interface is deliberately tiny — one class, one method, one exception —
so callers (today: the profile recommender; tomorrow: any CLI judgment)
never see provider details. No new runtime dependencies: httpx only.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["DecisionClient", "DecisionError", "DecisionResult"]


class _RedactHttpxUrls(logging.Filter):
    """Keep credential-bearing endpoints out of httpx's per-request INFO log.

    httpx logs ``HTTP Request: POST <full url>`` at INFO. SUNO_JEV_URL may carry
    ``user:password@`` or a ``?token=`` query, so any URL log argument that has
    userinfo, a query or a fragment is replaced by a stub.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._scrub(arg) for arg in record.args)
        return True

    @staticmethod
    def _scrub(value: object) -> object:
        text = str(value)
        if "://" in text and any(mark in text for mark in ("@", "?", "#")):
            return "<redacted-url>"
        return value


logging.getLogger("httpx").addFilter(_RedactHttpxUrls())

# MEASURED, DO NOT WIRE BLINDLY
# ---------------------------------------------------------------------------
# This client is compatible with the Needle service (hass-needle-app), and
# `raw_confidence` is the correct field to read. But a 17-question evaluation
# against the real suno-recommend use case measured:
#
#   grounded 15/17, correct 7  ->  47% precision
#   median raw_confidence 0.8517 on CORRECT answers
#   median raw_confidence 0.8745 on WRONG answers
#   four wrong answers scored >= 0.90
#   the deterministic fallback scored 4/4 where the model scored 2/4
#
# So raw_confidence is NOT a usefulness signal here, and lowering the threshold
# does not help: at 0.50 the model is still 46% correct, i.e. no better than
# its baseline. Treat any confidence from this provider as uncalibrated and
# uninformative about correctness, exactly as the service's own
# `confidence_semantics: needle_head_score_uncalibrated` declares.
#
# Consequence for this codebase: do not feed DecisionResult.confidence into a
# ranking weight or a blend such as `max(0.5, confidence)`. Doing so promotes
# the model's confidently-wrong answers. If this client is ever used, it is
# defensible only as a tie-breaker on scores the heuristic has already tied, and
# only with an explicit opt-in. See the evaluation for the full numbers.


class DecisionError(Exception):
    """The decision service is unreachable, misbehaving, or unusable.

    Callers are expected to treat this as "fall back to deterministic
    logic", never as a fatal condition.
    """


@dataclass(frozen=True)
class DecisionResult:
    """One answered question: the winning option and its confidence."""

    label: str
    confidence: float


def _winner_from_probabilities(
    probabilities: dict[str, Any], confidence_floor: float = 0.0
) -> DecisionResult | None:
    if not probabilities:
        return None
    best = max(probabilities, key=lambda k: float(probabilities[k]))
    return DecisionResult(
        label=str(best), confidence=max(confidence_floor, float(probabilities[best]))
    )


def _answer_to_result(node: dict[str, Any]) -> DecisionResult | None:
    """Normalize every known SystemOne answer spelling into a DecisionResult.

    Known shapes (live-verified 2026-09-28 unless noted):

    * ``{"label": "x", "confidence": 0.9}`` — TypeSafe Jev, local JEV fleet
    * ``{"label": "x", "raw_confidence": 0.9, "confidence_semantics":
      "needle_head_score_uncalibrated"}`` — Needle (schema-verified
      2026-09-29). The service deliberately ships no ``confidence``: the value
      is an uncalibrated model head score, not a probability, so reporting it
      under a probability-sounding key would mislead. It is read as-is, and
      ``confidence`` stays the fallback so the Jev spelling is unaffected.
    * ``{"choice": "x", "probabilities": {...}, "confidence": 0.667}`` —
      self-hosted Laya: winner under ``choice``, per-option probabilities;
      confidence is the max of the global value and the winner's share
    * ``{"probabilities": {...}}`` — probabilities without an explicit
      winner; argmax decides
    * ``{"noul": 0.78}`` — binary Noul questions; the number is the
      probability that the statement is true
    """
    if node.get("label"):
        return DecisionResult(
            label=str(node["label"]),
            confidence=float(node.get("raw_confidence", node.get("confidence", 0.5))),
        )
    choice = node.get("choice")
    if choice:
        global_confidence = float(node.get("confidence", 0.0))
        probabilities = node.get("probabilities") or {}
        own = float(probabilities.get(str(choice), 0.0))
        return DecisionResult(label=str(choice), confidence=max(global_confidence, own))
    probabilities = node.get("probabilities")
    if isinstance(probabilities, dict) and probabilities:
        return _winner_from_probabilities(probabilities)
    noul = node.get("noul")
    if isinstance(noul, (int, float)):
        return DecisionResult(label="yes", confidence=float(noul))
    return None


class DecisionClient:
    """Client for any SystemOne-shaped decision service.

    ``base_url`` is the full endpoint (e.g. ``http://127.0.0.1:8091/v1/decisions``
    or ``https://api.typesafe.ai/v1/systemone``). ``api_key`` is sent as a
    Bearer token when provided (hosted providers); it is never logged and
    never included in error messages.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _payload(self, state: str, question: str, options: list[str]) -> dict[str, Any]:
        questions: dict[str, Any] = {
            "pick": {
                "type": "choice",
                "instructions": question,
                "criteria": dict.fromkeys(options),
            }
        }
        payload: dict[str, Any] = {"state": state, "questions": questions}
        if self.model:
            payload["model"] = self.model
        return payload

    def ask(
        self,
        state: str,
        question: str,
        options: list[str],
        question_id: str = "pick",
    ) -> DecisionResult:
        """Ask one choice question; raise :class:`DecisionError` on any failure.

        ``options`` must contain at least two entries: single-option questions
        are rejected by several providers (Laya answers HTTP 422) and are
        meaningless as decisions — callers should fall back locally instead.
        """
        if len(options) < 2:
            raise DecisionError("a choice question needs at least two options")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = self._payload(state, question, options)
        if question_id != "pick":
            payload["questions"] = {question_id: payload["questions"]["pick"]}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.base_url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DecisionError(f"{type(exc).__name__} from the decision endpoint") from None
        # A successful HTTP response and valid JSON do not guarantee a valid
        # decision. Some backends return arrays, strings, or apology envelopes;
        # all must trigger the caller's deterministic fallback, not a crash.
        if not isinstance(body, dict):
            raise DecisionError("response was not a JSON object")
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise DecisionError("response carried no usable answers object")
        node = answers.get(question_id)
        if not isinstance(node, dict):
            raise DecisionError("response carried no usable answer node")
        try:
            result = _answer_to_result(node)
        except (AttributeError, ValueError, TypeError, OverflowError) as exc:
            raise DecisionError("response contained malformed answer values") from exc
        if result is None or result.label not in options:
            raise DecisionError(f"answer {result.label if result else '∅'!r} not among options")
        if not math.isfinite(result.confidence):
            raise DecisionError("response contained a non-finite confidence")
        return result
