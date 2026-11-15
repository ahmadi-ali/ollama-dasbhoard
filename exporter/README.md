# Ollama Exporter

A small FastAPI service that does two things:

1. **Polls the Ollama API** (`/api/version`, `/api/ps`, `/api/tags`) on every Prometheus scrape. It reports server health, loaded models, VRAM usage and installed models. **This works whatever port your clients use**, because nothing has to go through the proxy.
2. **Proxies Ollama requests.** Anything sent to this service (port `8000` by default) is forwarded to Ollama unchanged, including streaming responses (NDJSON and SSE). Along the way it records request counts, latency, time-to-first-token, token counts and generation speed.

> [!IMPORTANT]
> Request and token metrics (the **Proxy** metrics below) only count traffic that goes **through the exporter**. If your clients (Open WebUI, IDE plugins, scripts…) still call Ollama on `:11434` directly, those requests are invisible to the proxy. See [Capturing all traffic](#capturing-all-traffic).

---

## Exposed Metrics

### Poller metrics (always available)

| Metric | Type | Labels | Description |
| :--- | :--- | :--- | :--- |
| `ollama_up` | Gauge | – | `1` if the Ollama API is reachable from the exporter, otherwise `0` |
| `ollama_version_info` | Gauge | `version` | Ollama server version (value is always `1`) |
| `ollama_loaded_models` | Gauge | – | Number of models currently loaded in memory |
| `ollama_loaded_model_info` | Gauge | `model`, `family`, `parameter_size`, `quantization`, `format`, `runner` | Metadata of each loaded model (value is always `1`) |
| `ollama_loaded_model_size_bytes` | Gauge | `model` | Total memory used by a loaded model (weights + KV cache) |
| `ollama_loaded_model_vram_bytes` | Gauge | `model` | Part of the loaded model held in GPU VRAM |
| `ollama_loaded_model_ram_bytes` | Gauge | `model` | Part of the loaded model offloaded to system RAM / CPU (`size - size_vram`). Should be `0` for full-GPU speed |
| `ollama_loaded_model_context_length` | Gauge | `model` | Context window the model was loaded with |
| `ollama_loaded_model_expires_in_seconds` | Gauge | `model` | Seconds until the model is unloaded (keep-alive countdown) |
| `ollama_installed_models` | Gauge | – | Number of models available locally |
| `ollama_installed_model_size_bytes` | Gauge | `model` | On-disk size of each installed model |
| `ollama_installed_model_info` | Gauge | `model`, `family`, `parameter_size`, `quantization` | Metadata of each installed model (value is always `1`) |
| `ollama_poll_duration_seconds` | Gauge | – | Time taken to poll the Ollama API during the last scrape |

### Proxy metrics (traffic sent through the exporter)

| Metric | Type | Labels | Description |
| :--- | :--- | :--- | :--- |
| `ollama_requests_total` | Counter | `endpoint`, `model`, `status` | Requests proxied to Ollama, by HTTP status code |
| `ollama_request_errors_total` | Counter | `endpoint`, `model`, `reason` | Proxy-level failures: `upstream_unreachable` (Ollama down) or `stream_error` (connection broke mid-stream) |
| `ollama_requests_in_flight` | Gauge | `endpoint` | Requests currently being processed |
| `ollama_request_duration_seconds` | Histogram | `endpoint`, `model` | End-to-end request latency as seen by the proxy |
| `ollama_time_to_first_token_seconds` | Histogram | `endpoint`, `model` | Time until the first streamed chunk arrives (streaming requests only). Includes queueing, model load and prompt processing |
| `ollama_tokens_generated_total` | Counter | `model`, `type` | Tokens processed. `type="prompt"` = input tokens, `type="eval"` = output tokens |
| `ollama_tokens_per_request` | Histogram | `model`, `type` | Distribution of prompt / output tokens per request |
| `ollama_generation_tokens_per_second` | Histogram | `model` | Per-request output speed (`eval_count / eval_duration`) |
| `ollama_prompt_tokens_per_second` | Histogram | `model` | Per-request prompt-processing (prefill) speed (`prompt_eval_count / prompt_eval_duration`) |
| `ollama_model_load_duration_seconds` | Histogram | `model` | Model load time reported by Ollama. High values mean a cold load |
| `ollama_phase_duration_seconds_total` | Counter | `model`, `phase` | Total time Ollama spent per phase: `load`, `prompt_eval`, `eval`, `total` |

Histograms expose the usual `_bucket`, `_sum` and `_count` series. Counters expose `_total`.

**Label notes**

* `endpoint` is normalized to one of the known Ollama (see [openapi.yaml](openapi.yaml)) or OpenAI-compatible routes (`/api/generate`, `/api/chat`, `/api/embed`, `/api/embeddings`, `/api/tags`, `/api/ps`, `/api/show`, `/api/pull`, `/api/push`, `/api/create`, `/api/copy`, `/api/delete`, `/api/version`, `/api/blobs/{digest}`, `/api/usage`, `/api/balance`, `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`, `/v1/responses`, `/`). Any other path is reported as `other`, which keeps cardinality bounded.
* `model` comes from the request body (`model` field), or from the response if the request has none. It is `unknown` for requests without a model (e.g. `/api/tags`).
* Token metrics come from Ollama's native final stats (`/api/generate`, `/api/chat`, `/api/embed`) or from the OpenAI-style `usage` object (`/v1/*`). For streaming `/v1/chat/completions`, clients must send `"stream_options": {"include_usage": true}` to get token counts. Latency and request counts are recorded either way.

The standard `python_*` and `process_*` metrics from `prometheus_client` are exposed as well.

### Useful PromQL

```promql
# Output tokens/sec while generating, per model
sum by (model) (rate(ollama_tokens_generated_total{type="eval"}[5m]))
  / sum by (model) (rate(ollama_phase_duration_seconds_total{phase="eval"}[5m]))

# p95 time to first token
histogram_quantile(0.95, sum by (le) (rate(ollama_time_to_first_token_seconds_bucket[5m])))

# VRAM used by loaded models
sum(ollama_loaded_model_vram_bytes)

# Is any model spilling into system RAM (slow)?
sum(ollama_loaded_model_ram_bytes) > 0
```

---

## Endpoints

| Path | Description |
| :--- | :--- |
| `/metrics` | Prometheus metrics (also triggers the Ollama poll) |
| `/health` | Liveness check, used by the Docker `HEALTHCHECK` |
| `/*` | Everything else is proxied to `OLLAMA_HOST` |

## Configuration

| Environment variable | Default | Description |
| :--- | :--- | :--- |
| `OLLAMA_HOST` | `http://host.docker.internal:11434` | Upstream Ollama URL |
| `REQUEST_TIMEOUT_SECONDS` | `0` | Read timeout for proxied requests. `0` = no limit, since large models can take minutes |
| `CONNECT_TIMEOUT_SECONDS` | `10` | Connect timeout to Ollama |
| `POLL_TIMEOUT_SECONDS` | `5` | Timeout for each poll request during a scrape |
| `LOG_LEVEL` | `INFO` | Python log level |

In `docker-compose.yml`, `OLLAMA_HOST` is set from `OLLAMA_UPSTREAM` and the published port from `PROXY_PORT` (see below).

---

## Capturing all traffic

You have two options:

**A. Point clients at the proxy** (no change to Ollama): set your client's Ollama URL to `http://<host>:8000` instead of `http://<host>:11434`.

**B. Transparent mode** (no client changes): move Ollama to another port and let the exporter take over `11434`.

1. Make Ollama listen on `11435` (`sudo systemctl edit ollama.service`):
   ```ini
   [Service]
   Environment="OLLAMA_HOST=0.0.0.0:11435"
   ```
   ```bash
   sudo systemctl daemon-reload && sudo systemctl restart ollama
   ```
2. Create a `.env` file next to `docker-compose.yml`:
   ```dotenv
   OLLAMA_UPSTREAM=http://host.docker.internal:11435
   PROXY_PORT=11434
   ```
3. `docker compose up -d`

Every client that calls `:11434` now goes through the exporter. Note that in this mode, Ollama is unreachable on `:11434` while the exporter container is down. Tools that run on the host and call `ollama` (the CLI uses `OLLAMA_HOST`, default `127.0.0.1:11434`) also go through the proxy, so they keep working.

## Local development

```bash
pip install fastapi uvicorn httpx prometheus-client
OLLAMA_HOST=http://localhost:11434 uvicorn ollama_exporter:app --port 8000
curl -s localhost:8000/metrics | grep ^ollama_
```
