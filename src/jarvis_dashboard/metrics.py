"""Exact Prometheus exposition parsing and bounded upstream transport.

Structure and label decoding come from the maintained ``prometheus_client``
parser; the numeric **lexemes** for the four selected metric names are
preserved by a narrowly scoped line adapter (delivery plan D2 spike:
``prometheus_client`` exposes floats, and floats silently absorb fractional
values near 2^53, so accepted integers are taken from the raw text).

Transport rules (specification D2): standard-library HTTP only, redirects
never followed, each response bounded at 2 MiB, one total deadline per
request, sockets closed on all branches, upstream bodies never retained.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from urllib.parse import urlsplit

from prometheus_client.parser import text_string_to_metric_families

from .domain import (
    COMPUTED_INPUT,
    COUNTER_FOR_METRIC,
    GENERATED_OUTPUT,
    MAX_BODY_BYTES,
    MAX_EXACT_UPSTREAM,
    MAX_SAMPLES_PER_SCRAPE,
    MAX_SELECTED_IDENTITIES,
    METRIC_FOR_COUNTER,
    ScrapeCounters,
    ScrapeResult,
    canonical_labels_json,
)

# ---------------------------------------------------------------------------
# Exact numeric lexemes
# ---------------------------------------------------------------------------


class CounterValueError(ValueError):
    """A counter value lexeme is invalid; carries a stable payload-free code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_LEXEME_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_MAX_LEXEME_LEN = 64
_MAX_EXPONENT = 400  # guards exponent work far beyond the accepted range


def parse_exact_integer_lexeme(lexeme: str) -> int:
    """Parse a counter value lexeme with exact decimal semantics.

    Accepts unsigned decimal/scientific integers up to 2^53-1 (spec D3).
    Rejects negatives, fractional values, non-finite forms, hex floats,
    oversized exponents/lexemes and values above the exact-integer range.
    """
    text = lexeme.strip()
    lowered = text.lower().lstrip("+-")
    if lowered in ("nan", "inf", "infinity") or text.lower().startswith(("0x", "0X")):
        raise CounterValueError("non_finite_or_unsupported_number")
    if len(text) > _MAX_LEXEME_LEN or not _LEXEME_RE.match(text):
        raise CounterValueError("invalid_number")
    sign = -1 if text.startswith("-") else 1
    body = text.lstrip("+-")
    if "e" in body or "E" in body:
        mantissa, _, exponent_text = body.lower().partition("e")
    else:
        mantissa, exponent_text = body, ""
    if exponent_text:
        try:
            exponent = int(exponent_text)
        except ValueError as exc:  # regex already restricts this shape
            raise CounterValueError("invalid_number") from exc
        if abs(exponent) > _MAX_EXPONENT:
            raise CounterValueError("value_out_of_range")
    else:
        exponent = 0
    int_part, _, frac_part = mantissa.partition(".")
    digits = (int_part + frac_part) or "0"
    scaled = int(digits) * 10 ** (exponent - len(frac_part)) if exponent - len(frac_part) >= 0 else int(digits)
    if exponent - len(frac_part) < 0:
        shift = len(frac_part) - exponent
        quotient, remainder = divmod(int(digits), 10**shift)
        if remainder != 0:
            raise CounterValueError("fractional_value")
        scaled = quotient
    value = sign * scaled
    if value < 0:
        raise CounterValueError("negative_value")
    if value > MAX_EXACT_UPSTREAM:
        raise CounterValueError("above_exact_integer_range")
    return value


# ---------------------------------------------------------------------------
# Exposition parsing
# ---------------------------------------------------------------------------

_SAMPLE_LINE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?"
    r"\s+(?P<value>\S+)"
    r"(?:\s+(?P<ts>-?[0-9]+(?:\.[0-9]+)?))?\s*$"
)


@dataclass(frozen=True)
class RawSample:
    name: str
    raw_labels: str  # text between braces, "" when absent
    value_lexeme: str


def _scan_sample_lines(text: str) -> list[RawSample]:
    samples: list[RawSample] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _SAMPLE_LINE_RE.match(stripped)
        if match is None:
            # Unrecognised exposition text: the full-document parse below is
            # the authority on whether the scrape is structurally trustable.
            continue
        samples.append(
            RawSample(
                name=match.group("name"),
                raw_labels=match.group("labels") or "",
                value_lexeme=match.group("value"),
            )
        )
        if len(samples) > MAX_SAMPLES_PER_SCRAPE:
            raise ValueError("scrape_too_large")
    return samples


def _decode_labels(name: str, raw_labels: str) -> dict[str, str]:
    """Decode a label set via the official parser (no ad-hoc comma splitting)."""
    synthetic = f"{name}{{{raw_labels}}} 0" if raw_labels else f"{name} 0"
    for family in text_string_to_metric_families(synthetic):
        for sample in family.samples:
            return dict(sample.labels)
    raise ValueError("empty_label_parse")


def parse_scrape(text: str, observed_at: datetime) -> ScrapeResult:
    """Parse selected counters from exposition text.

    Whole-scrape rejections set ``scrape_error`` (no baseline changes). Per
    counter failures mark only that counter; a primary failure invalidates
    only the primary pair for that identity, and optional failures only their
    own counter (specification D3/D4).
    """
    try:
        families = list(text_string_to_metric_families(text))
    except Exception:
        return ScrapeResult(observed_at=observed_at, identities={}, scrape_error="exposition_invalid")
    try:
        samples = _scan_sample_lines(text)
    except ValueError:
        return ScrapeResult(observed_at=observed_at, identities={}, scrape_error="scrape_too_large")

    present_families: set[str] = set()
    for family in families:
        present_families.add(family.name)
        present_families.add(family.name + "_total")  # parser strips the suffix

    per_identity: dict[str, dict[str, str]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    duplicate_counters: set[tuple[str, str]] = set()  # (counter_key, identity)
    for raw in samples:
        counter_key = COUNTER_FOR_METRIC.get(raw.name)
        if counter_key is None:
            continue  # unrelated metric families never become series
        try:
            labels = _decode_labels(raw.name, raw.raw_labels)
            identity = canonical_labels_json(labels)
        except Exception:
            # Un-decodable labels cannot form an identity; the full-document
            # parse above is the authority on structural trust.
            continue
        pair = (raw.name, identity)
        if pair in seen_pairs:
            # Duplicate selected metric/identity samples are invalid, not summed.
            duplicate_counters.add((counter_key, identity))
            continue
        seen_pairs.add(pair)
        per_identity.setdefault(identity, {})[counter_key] = raw.value_lexeme

    identities: dict[str, ScrapeCounters] = {}
    for identity, by_counter in per_identity.items():
        labels = json.loads(identity)
        values: dict[str, int | None] = {}
        problems: dict[str, str] = {}
        for counter_key, metric_name in METRIC_FOR_COUNTER.items():
            entry = by_counter.get(counter_key)
            if entry is None:
                values[counter_key] = None
                problems[counter_key] = "missing"
                continue
            lexeme = entry
            try:
                values[counter_key] = parse_exact_integer_lexeme(lexeme)
            except CounterValueError as exc:
                values[counter_key] = None
                problems[counter_key] = exc.code
            if (counter_key, identity) in duplicate_counters:
                values[counter_key] = None
                problems[counter_key] = "duplicate_sample"
        # Coupled primary pair (specification D4): one invalid primary member
        # invalidates both; optional counters stay independent.
        if values.get(COMPUTED_INPUT) is None or values.get(GENERATED_OUTPUT) is None:
            for primary_key in (COMPUTED_INPUT, GENERATED_OUTPUT):
                if values.get(primary_key) is not None:
                    values[primary_key] = None
                    problems[primary_key] = "primary_pair_invalid"
        identities[identity] = ScrapeCounters(labels=labels, values=values, problems=problems)

    if len(identities) > MAX_SELECTED_IDENTITIES:
        return ScrapeResult(
            observed_at=observed_at,
            identities={},
            scrape_error="too_many_selected_identities",
        )

    missing = tuple(
        metric_name
        for metric_name in METRIC_FOR_COUNTER.values()
        if metric_name not in present_families and (metric_name + "_total") not in present_families
    )
    return ScrapeResult(observed_at=observed_at, identities=identities, missing_metrics=missing)


# ---------------------------------------------------------------------------
# Bounded transport
# ---------------------------------------------------------------------------


class UpstreamError(Exception):
    """Upstream fetch failure with a stable, payload-free code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NoRedirectHandler(urllib.request.BaseHandler):
    """Report redirects as upstream errors instead of following them (D2)."""

    def _reject(self, req, fp, code, msg, headers):
        raise UpstreamError("redirect_rejected")

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


@dataclass(frozen=True)
class FetchedStats:
    model: str
    uptime_s: float
    request_start: datetime
    response_end: datetime


class UpstreamFetcher:
    """Standard-library HTTP with a per-request deadline and a 2 MiB cap."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        *,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = __import__("time").monotonic,
    ):
        urlsplit(base_url)  # validated upstream by config; keep explicit
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._wall = wall_clock
        self._monotonic = monotonic
        # Built by hand: build_opener installs HTTPRedirectHandler ahead of
        # custom handlers, so redirect rejection needs an opener without it.
        opener = urllib.request.OpenerDirector()
        for handler in (
            urllib.request.ProxyHandler(),
            urllib.request.UnknownHandler(),
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(),
            urllib.request.HTTPDefaultErrorHandler(),
            urllib.request.HTTPErrorProcessor(),  # dispatches http_error_* chain
            _NoRedirectHandler(),
        ):
            opener.add_handler(handler)
        opener.addheaders = [("Accept", "text/plain; version=0.0.4")]
        self._opener = opener

    def _now(self) -> datetime:
        from datetime import UTC, datetime as _dt

        return (self._wall or (lambda: _dt.now(UTC)))()

    def _read_bounded(self, response) -> bytes:
        deadline = self._monotonic() + self._timeout
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise UpstreamError("deadline_exceeded")
            chunk = response.read(65536)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise UpstreamError("response_too_large")
            chunks.append(chunk)

    def fetch(self, path: str) -> bytes:
        url = self._base + path
        try:
            response = self._opener.open(url, timeout=self._timeout)
        except UpstreamError:
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise UpstreamError(f"http_status_{code}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, UpstreamError):
                raise reason
            if isinstance(reason, TimeoutError):
                raise UpstreamError("connect_timeout") from exc
            raise UpstreamError("connection_failed") from exc
        except TimeoutError as exc:
            # Response-header/body timeout: the per-request deadline was hit.
            raise UpstreamError("deadline_exceeded") from exc
        except OSError as exc:
            raise UpstreamError("transport_error") from exc
        try:
            return self._read_bounded(response)
        except UpstreamError:
            raise
        except TimeoutError as exc:
            raise UpstreamError("deadline_exceeded") from exc
        except OSError as exc:
            raise UpstreamError("transport_error") from exc
        finally:
            response.close()

    def fetch_metrics_text(self) -> str:
        body = self.fetch("/metrics")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UpstreamError("metrics_not_utf8") from exc

    def fetch_stats(self) -> FetchedStats:
        """Fetch /stats and record request start/end UTC for D4 evidence."""
        request_start = self._now()
        body = self.fetch("/stats")
        response_end = self._now()
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpstreamError("stats_invalid_json") from exc
        if not isinstance(document, dict):
            raise UpstreamError("stats_invalid_shape")
        model = document.get("model")
        uptime = document.get("uptime_s")
        if not isinstance(model, str) or not model:
            raise UpstreamError("stats_missing_model")
        if isinstance(uptime, bool) or not isinstance(uptime, (int, float)) or not math.isfinite(uptime) or uptime < 0:
            raise UpstreamError("stats_invalid_uptime")
        return FetchedStats(model=model, uptime_s=float(uptime), request_start=request_start, response_end=response_end)
