"""
MaRDI Portal status service.

Queries Traefik metrics and the public SPARQL endpoint, and serves the results
as JSON for the status page to render.

Metrics are read either through Grafana's datasource proxy (default, uses a
service-account token) or directly from Prometheus. The PromQL is identical
in both cases -- only the transport differs.

Config via environment variables -- see README.md.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, send_from_directory

# ---------------------------------------------------------------- config

# "grafana" routes queries through Grafana's /api/ds/query with a
# service-account token. "prometheus" talks to Prometheus directly, which
# needs network access to it (ZIB VPN, or running inside the cluster).
METRICS_BACKEND = os.getenv("METRICS_BACKEND", "grafana").lower()

GRAFANA_URL = os.getenv("GRAFANA_URL", "https://grafana-mardi.zib.de").rstrip("/")
GRAFANA_TOKEN = os.getenv("GRAFANA_TOKEN", "")
# The datasource's uid, as it appears in Grafana panel JSON.
GRAFANA_DATASOURCE_UID = os.getenv("GRAFANA_DATASOURCE_UID", "prometheus")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "https://prometheus-mardi.zib.de").rstrip("/")

SPARQL_URL = os.getenv("SPARQL_URL", "https://query.portal.mardi4nfdi.de/sparql")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "30"))
QUERY_TIMEOUT = int(os.getenv("QUERY_TIMEOUT_SECONDS", "10"))
EDIT_WINDOW_DAYS = int(os.getenv("EDIT_WINDOW_DAYS", "7"))

# Traefik label selectors. `websecure` is the public HTTPS entrypoint.
ENTRYPOINT = os.getenv("TRAEFIK_ENTRYPOINT", "websecure")
JOB = os.getenv("TRAEFIK_JOB", "traefik")

# Traefik service labels arrive as `production-<name>-<deployhash>@docker`.
# This strips the hash so `wikibase` stays one series across redeploys.
# NOTE: label_replace runs *inside* the sum, so series from different deploy
# generations merge correctly. Doing it the other way round leaves duplicate
# series with identical names after a redeploy.
SERVICE_RE = r"production-(.+)-[a-f0-9]{16,}@.*"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("mardi-status")

if METRICS_BACKEND not in ("grafana", "prometheus"):
    raise SystemExit(
        f"METRICS_BACKEND must be 'grafana' or 'prometheus', got {METRICS_BACKEND!r}"
    )

if METRICS_BACKEND == "grafana" and not GRAFANA_TOKEN:
    # Fail at startup rather than serving a permanently half-empty page that
    # nobody notices is broken.
    raise SystemExit(
        "GRAFANA_TOKEN is not set. Create a service account token in Grafana "
        "(Administration -> Service accounts), or set METRICS_BACKEND=prometheus."
    )

app = Flask(__name__, static_folder="static")


# ---------------------------------------------------------------- queries

def entrypoint_rate(extra_labels: str = "") -> str:
    """rate() over the public entrypoint's request counter."""
    labels = 'job="{}", entrypoint="{}"{}'.format(JOB, ENTRYPOINT, extra_labels)
    return "sum(rate(traefik_entrypoint_requests_total{%s}[5m]))" % labels


def entrypoint_quantile(q: float) -> str:
    labels = 'job="{}", entrypoint="{}"'.format(JOB, ENTRYPOINT)
    return (
        "histogram_quantile(%s, sum(rate("
        "traefik_entrypoint_request_duration_seconds_bucket{%s}[5m])) by (le))"
        % (q, labels)
    )


def service_rate(extra_labels: str = "") -> str:
    """
    Per-service rate, keyed by service name with the deploy hash stripped.

    The captured name goes into a *new* label (`svc`) rather than overwriting
    `exported_service`. Overwriting collapses distinct series into identical
    labelsets, which PromQL rejects outright ("vector cannot contain metrics
    with the same labelset") -- two Traefik entries can share a name when
    deploy generations overlap, or when one service is registered by more
    than one provider. Keeping the original label intact means the vector
    stays unique and the outer sum does the merging.

    The inner label_replace copies exported_service to svc for everything;
    the outer one overwrites svc with the captured name where the deploy
    pattern matches. Services not following that pattern keep their raw name
    rather than vanishing.
    """
    labels = 'job="{}"{}'.format(JOB, extra_labels)
    return (
        "sum by (svc) (label_replace(label_replace("
        "rate(traefik_service_requests_total{%s}[5m]), "
        '"svc", "$1", "exported_service", "(.*)"), '
        '"svc", "$1", "exported_service", "%s"))' % (labels, SERVICE_RE)
    )


# 403s are CrowdSec bot blocks. They have run several times higher than all
# real traffic, so counting them would make the public number track bot waves
# rather than portal health. Reported separately instead.
REAL_TRAFFIC = ', code!="403"'

# Queries returning a single value
PROM_QUERIES = {
    "request_rate": entrypoint_rate(REAL_TRAFFIC),
    "blocked_rate": entrypoint_rate(', code="403"'),
    # clamp_min avoids divide-by-zero when there is no traffic at all
    "error_rate_pct": "%s / clamp_min(%s, 0.001) * 100" % (
        entrypoint_rate(', code=~"5.."'),
        entrypoint_rate(REAL_TRAFFIC),
    ),
    "latency_p50": entrypoint_quantile(0.50),
    "latency_p95": entrypoint_quantile(0.95),
    "latency_p99": entrypoint_quantile(0.99),
}

# Queries returning one value per service
PROM_VECTOR_QUERIES = {
    "service_request_rate": service_rate(),
    "service_error_rate": service_rate(', code=~"5.."'),
}


# ---------------------------------------------------------------- backends
#
# Both backends return the same shape:
#     {query_key: [(labels_dict, float_value), ...]}
# so the rest of the app does not care which one is in use.

def _clean(value):
    """Reject NaN, which histogram_quantile returns for empty buckets."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def query_via_prometheus(queries):
    """
    One HTTP call per query against Prometheus /api/v1/query.

    A query Prometheus refuses (422) is logged and returns empty rather than
    raising, so one malformed query cannot blank out every other metric.
    Network failures still raise -- those mean the backend itself is gone.
    """
    out = {}
    for key, promql in queries.items():
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=QUERY_TIMEOUT,
        )
        if r.status_code == 422:
            # Prometheus puts the reason in the body; raise_for_status drops it
            log.warning("query %r rejected: %s", key, r.text[:300])
            out[key] = []
            continue
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"prometheus returned {payload.get('status')} for {key}")

        series = []
        for item in payload["data"]["result"]:
            value = _clean(item["value"][1])
            if value is not None:
                series.append((item.get("metric", {}), value))
        out[key] = series
    return out


def _frames_to_series(frames):
    """
    Unpack Grafana's dataframe format.

    A frame's `data.values` is column-oriented: values[0] is the time column,
    values[1] the value column. Labels live on the value column's field
    definition. One frame per series.
    """
    series = []
    for frame in frames or []:
        fields = frame.get("schema", {}).get("fields", [])
        columns = frame.get("data", {}).get("values", [])
        if len(fields) < 2 or len(columns) < 2:
            continue
        labels = fields[1].get("labels") or {}
        column = columns[1]
        if not column:
            continue
        # Take the most recent point. Instant queries return one, but range
        # results are tolerated so a config change cannot silently break this.
        value = _clean(column[-1])
        if value is not None:
            series.append((labels, value))
    return series


def query_via_grafana(queries):
    """
    All queries in a single POST to Grafana's datasource proxy.

    Grafana forwards the PromQL to Prometheus and returns dataframes. Batching
    means one round trip instead of one per query.
    """
    keys = list(queries)
    # refIds must be unique within the request; map them back afterwards.
    ref_for = {key: "Q%d" % i for i, key in enumerate(keys)}

    body = {
        "queries": [
            {
                "refId": ref_for[key],
                "expr": queries[key],
                "instant": True,
                "range": False,
                "datasource": {"type": "prometheus", "uid": GRAFANA_DATASOURCE_UID},
                "intervalMs": 30000,
                "maxDataPoints": 1,
            }
            for key in keys
        ],
        "from": "now-5m",
        "to": "now",
    }

    r = requests.post(
        "%s/api/ds/query" % GRAFANA_URL,
        json=body,
        headers={
            "Authorization": "Bearer %s" % GRAFANA_TOKEN,
            "Content-Type": "application/json",
        },
        timeout=QUERY_TIMEOUT,
    )
    if r.status_code in (401, 403):
        # Distinguish a bad token from Grafana being down -- they need
        # different fixes and look identical in a generic error.
        raise RuntimeError(
            "Grafana rejected the token (HTTP %d). Check GRAFANA_TOKEN is valid "
            "and the service account has Viewer access." % r.status_code
        )
    r.raise_for_status()

    results = r.json().get("results", {})
    out = {}
    for key in keys:
        entry = results.get(ref_for[key], {})
        if entry.get("error"):
            raise RuntimeError("grafana query %r failed: %s" % (key, entry["error"]))
        out[key] = _frames_to_series(entry.get("frames"))
    return out


def run_queries(queries):
    if METRICS_BACKEND == "grafana":
        return query_via_grafana(queries)
    return query_via_prometheus(queries)


# ---------------------------------------------------------------- collection

def collect_metrics():
    """All Traefik-derived metrics. Raises on transport failure."""
    results = run_queries(dict(PROM_QUERIES, **PROM_VECTOR_QUERIES))

    data = {}
    for key in PROM_QUERIES:
        series = results.get(key) or []
        data[key] = series[0][1] if series else None

    for key in PROM_VECTOR_QUERIES:
        data[key] = {
            labels["svc"]: value
            for labels, value in (results.get(key) or [])
            if labels.get("svc")
        }

    return data


def sparql_query(query):
    r = requests.get(
        SPARQL_URL,
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=QUERY_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def collect_sparql():
    """SPARQL endpoint health and recent edit count."""
    start = time.perf_counter()
    sparql_query("SELECT (1 AS ?ping) WHERE {}")
    latency_ms = round((time.perf_counter() - start) * 1000)

    since = (datetime.now(timezone.utc) - timedelta(days=EDIT_WINDOW_DAYS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    # NOTE: Blazegraph does not reliably support xsd:duration arithmetic with
    # NOW(), so the cutoff is computed here and inlined as a literal.
    edits = sparql_query("""
        SELECT (COUNT(?item) AS ?recentEdits) WHERE {
          ?item <http://schema.org/dateModified> ?date .
          FILTER(?date > "%s"^^<http://www.w3.org/2001/XMLSchema#dateTime>)
        }
    """ % since)
    bindings = edits["results"]["bindings"]
    count = int(bindings[0]["recentEdits"]["value"]) if bindings else 0

    return {
        "latency_ms": latency_ms,
        "recent_edits": count,
        "edit_window_days": EDIT_WINDOW_DAYS,
    }


def collect():
    """
    Build the full status payload.

    Each source is collected independently so one failure degrades that
    section only -- a Grafana outage should not blank out SPARQL data.
    """
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": METRICS_BACKEND,
        "sources": {},
    }

    try:
        payload["prometheus"] = collect_metrics()
        payload["sources"]["prometheus"] = "ok"
    except Exception as exc:
        log.warning("metrics collection failed: %s", exc)
        payload["prometheus"] = None
        payload["sources"]["prometheus"] = "unavailable"

    try:
        payload["sparql"] = collect_sparql()
        payload["sources"]["sparql"] = "ok"
    except Exception as exc:
        log.warning("sparql collection failed: %s", exc)
        payload["sparql"] = None
        payload["sources"]["sparql"] = "unavailable"

    return payload


# ---------------------------------------------------------------- cache

class Cache:
    """
    Serves a cached payload, refreshed at most once per TTL.

    This matters more than it looks. A status page gets its heaviest traffic
    during an outage -- which is exactly when the monitoring stack is already
    under stress. Without caching, every visitor would trigger a fresh round
    of queries at the worst possible moment.

    On refresh failure the previous payload is kept and marked stale, so the
    page can show last-known-good data with an honest timestamp rather than
    nothing at all.
    """

    def __init__(self, ttl):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._payload = None
        self._fetched_at = 0.0

    def get(self):
        with self._lock:
            if self._payload is not None and time.time() - self._fetched_at < self.ttl:
                return self._payload

            try:
                self._payload = collect()
                self._fetched_at = time.time()
            except Exception as exc:
                log.error("collection failed entirely: %s", exc)
                if self._payload is None:
                    raise
                self._payload = dict(self._payload, stale=True)

            return self._payload


cache = Cache(CACHE_TTL)


# ---------------------------------------------------------------- routes

@app.route("/api/status.json")
def status_json():
    try:
        payload = cache.get()
    except Exception:
        return jsonify({"error": "status data unavailable"}), 503
    response = jsonify(payload)
    response.headers["Cache-Control"] = "public, max-age=%d" % CACHE_TTL
    return response


@app.route("/healthz")
def healthz():
    """Liveness only -- deliberately does not depend on upstream sources."""
    return "ok", 200


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    log.info("metrics backend: %s", METRICS_BACKEND)
    if METRICS_BACKEND == "grafana":
        log.info("grafana: %s (datasource uid=%s)", GRAFANA_URL, GRAFANA_DATASOURCE_UID)
    else:
        log.info("prometheus: %s", PROMETHEUS_URL)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
