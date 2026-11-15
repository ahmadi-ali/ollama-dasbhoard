"""Ollama Prometheus exporter + transparent proxy.

Two independent sources of metrics:

1. Poller (always on): on every Prometheus scrape the exporter queries the Ollama
   API (/api/version, /api/ps, /api/tags) and reports server health, loaded models,
   VRAM usage and installed models. Works even if clients talk to Ollama directly.

2. Proxy (opt-in per client): every request sent to this service is forwarded to
   Ollama. Streaming (NDJSON / SSE) and non-streaming responses are passed through
   untouched while request counts, latency, time-to-first-token, token counts and
   generation speed are recorded.
"""

import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434").rstrip("/")
POLL_TIMEOUT = float(os.getenv("POLL_TIMEOUT_SECONDS", "5"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT_SECONDS", "10"))
# Read timeout for proxied requests. 0 = no limit (large models can take minutes).
READ_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "0")) or None

log = logging.getLogger("ollama_exporter")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logging.getLogger("httpx").setLevel(logging.WARNING)

# Endpoints are normalised to keep label cardinality bounded.
# Reference: ./openapi.yaml (https://github.com/ollama/ollama/blob/main/docs/openapi.yaml)
# plus OpenAI-compatible (/v1/*) and legacy (/api/embeddings) routes served by Ollama.
KNOWN_ENDPOINTS = {
    "/", "/api/generate", "/api/chat", "/api/embed", "/api/embeddings",
    "/api/tags", "/api/ps", "/api/show", "/api/pull", "/api/push",
    "/api/create", "/api/copy", "/api/delete", "/api/version",
    "/api/blobs/{digest}", "/api/usage", "/api/balance",
    "/v1/chat/completions", "/v1/completions", "/v1/embeddings",
    "/v1/models", "/v1/responses",
}
INFERENCE_ENDPOINTS = {
    "/api/generate", "/api/chat", "/api/embed", "/api/embeddings",
    "/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/responses",
}

# Hop-by-hop / transport headers that must not be forwarded verbatim.
REQUEST_HEADER_DROP = {
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "accept-encoding", "upgrade", "proxy-connection", "te", "trailer",
}
RESPONSE_HEADER_DROP = {
    "content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding",
}

# --------------------------------------------------------------------------- #
# Proxy metrics (populated only by traffic that goes through this service)
# --------------------------------------------------------------------------- #
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600)
TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)
TPS_BUCKETS = (1, 2, 5, 10, 15, 20, 30, 40, 50, 75, 100, 150, 200, 500, 1000, 2000, 5000)
TOKEN_BUCKETS = (16, 64, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072)

REQUEST_COUNT = Counter(
    "ollama_requests_total", "Requests proxied to Ollama", ["endpoint", "model", "status"])
REQUEST_ERRORS = Counter(
    "ollama_request_errors_total", "Proxy-level failures (upstream unreachable, broken stream)",
    ["endpoint", "model", "reason"])
IN_FLIGHT = Gauge(
    "ollama_requests_in_flight", "Requests currently being processed", ["endpoint"])
REQUEST_LATENCY = Histogram(
    "ollama_request_duration_seconds", "End-to-end request latency as seen by the proxy",
    ["endpoint", "model"], buckets=LATENCY_BUCKETS)
TTFT = Histogram(
    "ollama_time_to_first_token_seconds", "Time until the first streamed chunk is received",
    ["endpoint", "model"], buckets=TTFT_BUCKETS)
TOKENS = Counter(
    "ollama_tokens_generated_total", "Tokens processed (type=prompt|eval)", ["model", "type"])
TOKENS_PER_REQUEST = Histogram(
    "ollama_tokens_per_request", "Tokens per request (type=prompt|eval)",
    ["model", "type"], buckets=TOKEN_BUCKETS)
GEN_SPEED = Histogram(
    "ollama_generation_tokens_per_second", "Per-request output speed (eval_count / eval_duration)",
    ["model"], buckets=TPS_BUCKETS)
PROMPT_SPEED = Histogram(
    "ollama_prompt_tokens_per_second",
    "Per-request prompt processing speed (prompt_eval_count / prompt_eval_duration)",
    ["model"], buckets=TPS_BUCKETS)
LOAD_DURATION = Histogram(
    "ollama_model_load_duration_seconds", "Model load time reported by Ollama (load_duration)",
    ["model"], buckets=(0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120))
PHASE_SECONDS = Counter(
    "ollama_phase_duration_seconds_total",
    "Cumulative time Ollama spent per phase (phase=load|prompt_eval|eval|total)",
    ["model", "phase"])


def normalize_endpoint(path: str) -> str:
    if path in KNOWN_ENDPOINTS:
        return path
    if path.startswith("/api/blobs/"):
        return "/api/blobs/{digest}"
    if path.startswith("/v1/models/"):
        return "/v1/models"
    return "other"


# --------------------------------------------------------------------------- #
# Poller: custom collector that queries the Ollama API on each scrape
# --------------------------------------------------------------------------- #
_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _parse_ts(value: str):
    """Parse Ollama RFC3339 timestamps (nanosecond precision) to datetime."""
    try:
        value = _FRAC_RE.sub(r"\1", value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except Exception:
        return None


class OllamaPoller:
    def __init__(self):
        self.client = httpx.Client(base_url=OLLAMA_HOST, timeout=POLL_TIMEOUT)

    def _get(self, path):
        r = self.client.get(path)
        r.raise_for_status()
        return r.json()

    def collect(self):
        start = time.perf_counter()
        up = GaugeMetricFamily("ollama_up", "1 if the Ollama API is reachable, else 0")
        version = GaugeMetricFamily("ollama_version_info", "Ollama server version", labels=["version"])
        loaded = GaugeMetricFamily("ollama_loaded_models", "Number of models currently loaded in memory")
        l_info = GaugeMetricFamily(
            "ollama_loaded_model_info", "Metadata of loaded models (value is always 1)",
            labels=["model", "family", "parameter_size", "quantization", "format", "runner"])
        l_size = GaugeMetricFamily("ollama_loaded_model_size_bytes", "Total memory used by a loaded model", labels=["model"])
        l_vram = GaugeMetricFamily("ollama_loaded_model_vram_bytes", "VRAM used by a loaded model", labels=["model"])
        l_ram = GaugeMetricFamily("ollama_loaded_model_ram_bytes", "System RAM used by a loaded model (size - size_vram, i.e. CPU offload)", labels=["model"])
        l_ctx = GaugeMetricFamily("ollama_loaded_model_context_length", "Context window the model was loaded with", labels=["model"])
        l_exp = GaugeMetricFamily("ollama_loaded_model_expires_in_seconds", "Seconds until the model is unloaded (keep-alive)", labels=["model"])
        installed = GaugeMetricFamily("ollama_installed_models", "Number of models available locally")
        i_size = GaugeMetricFamily("ollama_installed_model_size_bytes", "On-disk size of an installed model", labels=["model"])
        i_info = GaugeMetricFamily(
            "ollama_installed_model_info", "Metadata of installed models (value is always 1)",
            labels=["model", "family", "parameter_size", "quantization"])

        ok = 1
        try:
            v = self._get("/api/version")
            version.add_metric([v.get("version", "unknown")], 1)

            ps = self._get("/api/ps").get("models") or []
            loaded.add_metric([], len(ps))
            now = datetime.now(timezone.utc)
            for m in ps:
                name = m.get("name") or m.get("model", "unknown")
                d = m.get("details") or {}
                size = float(m.get("size", 0))
                vram = float(m.get("size_vram", 0))
                l_info.add_metric([
                    name, d.get("family", ""), d.get("parameter_size", ""),
                    d.get("quantization_level", ""), d.get("format", ""), m.get("runner", ""),
                ], 1)
                l_size.add_metric([name], size)
                l_vram.add_metric([name], vram)
                l_ram.add_metric([name], max(size - vram, 0))
                if m.get("context_length"):
                    l_ctx.add_metric([name], float(m["context_length"]))
                exp = _parse_ts(m.get("expires_at", ""))
                if exp is not None:
                    l_exp.add_metric([name], max((exp - now).total_seconds(), 0))

            tags = self._get("/api/tags").get("models") or []
            installed.add_metric([], len(tags))
            for m in tags:
                name = m.get("name") or m.get("model", "unknown")
                d = m.get("details") or {}
                i_size.add_metric([name], float(m.get("size", 0)))
                i_info.add_metric([
                    name, d.get("family", ""), d.get("parameter_size", ""), d.get("quantization_level", ""),
                ], 1)
        except Exception as e:  # noqa: BLE001
            ok = 0
            log.warning("Ollama poll failed: %s", e)

        up.add_metric([], ok)
        yield up
        if ok:
            yield from (version, loaded, l_info, l_size, l_vram, l_ram, l_ctx, l_exp,
                        installed, i_size, i_info)
        poll = GaugeMetricFamily("ollama_poll_duration_seconds", "Time taken to poll the Ollama API")
        poll.add_metric([], time.perf_counter() - start)
        yield poll


REGISTRY.register(OllamaPoller())


# --------------------------------------------------------------------------- #
# Stream tracker: parses the response body as it flows to the client
# --------------------------------------------------------------------------- #
class StreamTracker:
    def __init__(self, endpoint, model, start, status, streaming):
        self.endpoint = endpoint
        self.model = model
        self.start = start
        self.status = status
        self.streaming = streaming
        self.track_tokens = endpoint in INFERENCE_ENDPOINTS and status < 400
        self.buf = b""
        self.first_chunk_at = None
        self.stats = None   # final Ollama-native stats object
        self.usage = None   # OpenAI-compatible usage object

    def feed(self, chunk: bytes):
        """Never raises: metrics parsing must not break the proxied stream."""
        try:
            self._feed(chunk)
        except Exception as e:  # noqa: BLE001
            self.track_tokens = False
            log.warning("Metrics parsing disabled for this request: %s", e)

    def _feed(self, chunk: bytes):
        if not chunk:
            return
        if self.first_chunk_at is None:
            self.first_chunk_at = time.perf_counter()
        if not self.track_tokens:
            return
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            self._parse_line(line)

    def _parse_line(self, line: bytes):
        line = line.strip()
        if not line:
            return
        if line.startswith(b"data:"):          # SSE (OpenAI-compatible streaming)
            line = line[5:].strip()
            if line == b"[DONE]":
                return
        elif not line.startswith(b"{"):
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        if self.model == "unknown" and isinstance(obj.get("model"), str):
            self.model = obj["model"]
        if obj.get("done") or "eval_count" in obj or "prompt_eval_count" in obj:
            self.stats = obj
        usage = obj.get("usage")
        if not isinstance(usage, dict) and isinstance(obj.get("response"), dict):
            usage = obj["response"].get("usage")  # /v1/responses "response.completed" event
        if isinstance(usage, dict):
            self.usage = usage

    def finish(self):
        if self.track_tokens and self.buf:
            self._parse_line(self.buf)
            self.buf = b""
        end = time.perf_counter()
        m, ep = self.model, self.endpoint
        REQUEST_COUNT.labels(ep, m, str(self.status)).inc()
        REQUEST_LATENCY.labels(ep, m).observe(end - self.start)
        if self.streaming and self.first_chunk_at is not None and self.status < 400:
            TTFT.labels(ep, m).observe(self.first_chunk_at - self.start)

        prompt_tokens = eval_tokens = None
        if self.stats:
            s = self.stats
            prompt_tokens = s.get("prompt_eval_count")
            eval_tokens = s.get("eval_count")
            ns = 1e9
            for key, phase in (("load_duration", "load"), ("prompt_eval_duration", "prompt_eval"),
                               ("eval_duration", "eval"), ("total_duration", "total")):
                if s.get(key):
                    PHASE_SECONDS.labels(m, phase).inc(s[key] / ns)
            if s.get("load_duration"):
                LOAD_DURATION.labels(m).observe(s["load_duration"] / ns)
            if eval_tokens and s.get("eval_duration"):
                GEN_SPEED.labels(m).observe(eval_tokens / (s["eval_duration"] / ns))
            if prompt_tokens and s.get("prompt_eval_duration"):
                PROMPT_SPEED.labels(m).observe(prompt_tokens / (s["prompt_eval_duration"] / ns))
        elif self.usage:
            u = self.usage
            prompt_tokens = u.get("prompt_tokens", u.get("input_tokens"))
            eval_tokens = u.get("completion_tokens", u.get("output_tokens"))

        for kind, value in (("prompt", prompt_tokens), ("eval", eval_tokens)):
            if isinstance(value, (int, float)) and value > 0:
                TOKENS.labels(m, kind).inc(value)
                TOKENS_PER_REQUEST.labels(m, kind).observe(value)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=None, pool=None),
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=50),
    )
    log.info("Proxying to %s", OLLAMA_HOST)
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/metrics")
def metrics():
    # Sync handler -> runs in a threadpool, so the blocking poll does not stall the proxy.
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"status": "healthy", "upstream": OLLAMA_HOST}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(path: str, request: Request):
    raw_path = "/" + path
    endpoint = normalize_endpoint(raw_path)
    body = await request.body()

    model = "unknown"
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                model = str(data.get("model") or data.get("name") or "unknown")
        except ValueError:
            pass

    url = f"{OLLAMA_HOST}{raw_path}"
    if request.url.query:
        url += f"?{request.url.query}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in REQUEST_HEADER_DROP}

    client: httpx.AsyncClient = request.app.state.client
    start = time.perf_counter()
    IN_FLIGHT.labels(endpoint).inc()
    try:
        upstream = await client.send(
            client.build_request(request.method, url, headers=headers, content=body), stream=True)
    except httpx.HTTPError as e:
        IN_FLIGHT.labels(endpoint).dec()
        REQUEST_ERRORS.labels(endpoint, model, "upstream_unreachable").inc()
        REQUEST_COUNT.labels(endpoint, model, "502").inc()
        log.warning("Upstream error for %s: %s", raw_path, e)
        return JSONResponse({"error": f"ollama upstream error: {e}"}, status_code=502)

    ctype = upstream.headers.get("content-type", "")
    streaming = "ndjson" in ctype or "event-stream" in ctype
    tracker = StreamTracker(endpoint, model, start, upstream.status_code, streaming)

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes():
                tracker.feed(chunk)
                yield chunk
        except Exception as e:  # noqa: BLE001
            REQUEST_ERRORS.labels(endpoint, tracker.model, "stream_error").inc()
            log.warning("Stream error for %s: %s", raw_path, e)
            raise
        finally:
            await upstream.aclose()
            IN_FLIGHT.labels(endpoint).dec()
            try:
                tracker.finish()
            except Exception as e:  # noqa: BLE001
                log.warning("Failed to record metrics for %s: %s", raw_path, e)

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in RESPONSE_HEADER_DROP}
    return StreamingResponse(body_iter(), status_code=upstream.status_code, headers=resp_headers)
