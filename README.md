# MaRDI Portal Status Service

A small containerised webserver that queries Prometheus (Traefik metrics) and
the public SPARQL endpoint, and serves a status page for the MaRDI portal.

Implements MaRDIRoadmap#130.

## How it works

    browser ──> status service ──> Prometheus  (Traefik metrics, ZIB-internal)
                              └──> SPARQL      (query.portal.mardi4nfdi.de)

The browser never queries Prometheus directly: it sits behind the ZIB VPN and
is not reachable from outside. All queries happen server-side.

Results are cached (default 30s). This matters more than it looks — a status
page gets its heaviest traffic during an outage, which is exactly when the
monitoring stack is already under load. Caching means a traffic spike does not
turn into a query storm against Prometheus.

## Endpoints

| Path                | Purpose                                    |
|---------------------|--------------------------------------------|
| `/`                 | The status page                            |
| `/api/status.json`  | All metrics as JSON                        |
| `/healthz`          | Liveness probe (no upstream dependencies)  |

## Configuration

| Variable                  | Default                                     |
|---------------------------|---------------------------------------------|
| `PROMETHEUS_URL`          | `http://prometheus:9090`                    |
| `SPARQL_URL`              | `https://query.portal.mardi4nfdi.de/sparql` |
| `CACHE_TTL_SECONDS`       | `30`                                        |
| `QUERY_TIMEOUT_SECONDS`   | `10`                                        |
| `EDIT_WINDOW_DAYS`        | `7`                                         |
| `TRAEFIK_ENTRYPOINT`      | `websecure`                                 |
| `TRAEFIK_JOB`             | `traefik`                                   |

`PROMETHEUS_URL` is the one that must be confirmed for production — from
inside the cluster it is likely `http://prometheus:9090`, but ask whoever
maintains the stack rather than guessing.

## Run locally

Requires the ZIB VPN, since Prometheus is not publicly reachable.

    pip install -r requirements.txt
    PROMETHEUS_URL=https://prometheus-mardi.zib.de python app.py
    # http://localhost:8080

Without the VPN the page still loads and shows SPARQL metrics; the Prometheus
cards render as unavailable. That degradation is deliberate.

## Build and run the container

    docker build -t mardi-status .
    docker run -p 8080:8080 -e PROMETHEUS_URL=http://prometheus:9090 mardi-status

## Deploying into the portal stack

Traefik labels, if deployed via Compose:

    labels:
      - traefik.enable=true
      - traefik.http.routers.status.rule=Host(`status.portal.mardi4nfdi.de`)
      - traefik.http.routers.status.entrypoints=websecure
      - traefik.http.routers.status.tls.certresolver=letsencrypt
      - traefik.http.services.status.loadbalancer.server.port=8080

The service needs to reach Prometheus on the internal network, and should be
excluded from CrowdSec blocking rules so the page stays reachable during an
incident.

## Known limitations

- **Runs inside ZIB.** If the portal goes down with the infrastructure, this
  page goes down too — which is when people would want it. Wikimedia host
  wikimediastatus.net externally for exactly this reason. Acceptable for a
  first version; worth revisiting. Moving the page out later does not require
  changing the collection logic, only where the JSON is published.
- **No incident history.** The "Past Incidents" section is static markup.
- **No uptime history or alerting.**
- **Time-window tabs (24h/7d/30d) are inert.** The service exposes a single
  5-minute window; range queries would need adding.

## Notes on the metrics

`request_rate` excludes HTTP 403. Those are CrowdSec bot blocks, and they have
run at several times the volume of all real traffic — including them would
make the headline number track bot waves rather than portal health. They are
reported separately as `blocked_rate`.

Per-service queries apply `label_replace` *inside* the `sum`, so that series
from different deploy generations merge into one. Several panels on the
Grafana dashboard do this the other way round, which leaves duplicate series
with identical legend names after a redeploy.
