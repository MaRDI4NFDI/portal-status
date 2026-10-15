# MaRDI Portal Status Service

A small containerised webserver that queries Prometheus (Traefik metrics) and
the public SPARQL endpoint, and serves a status page for the MaRDI portal.

Implements MaRDIRoadmap#130.

## How it works

    browser ──> status service ──> Grafana ──> Prometheus   (Traefik metrics)
                              └──> SPARQL                   (public endpoint)

The browser never queries the monitoring stack directly: Prometheus sits
behind the ZIB VPN and is not reachable from outside. All queries happen
server-side.

Metrics go through Grafana's datasource proxy rather than Prometheus directly.
Prometheus has no authentication of its own — access is all-or-nothing at the
network level — whereas Grafana issues scoped service-account tokens that can
be granted and revoked without touching network config. The PromQL is
identical either way; only the transport differs.

The tradeoff is one more link in the chain: if Grafana is down but Prometheus
is healthy, metrics go unavailable. Set `METRICS_BACKEND=prometheus` to bypass
Grafana if that happens.

Results are cached (default 30s). This matters more than it looks — a status
page gets its heaviest traffic during an outage, which is exactly when the
monitoring stack is already under load. Caching means a traffic spike does not
turn into a query storm against Prometheus.

## What it reports

A traffic light per service plus one overall verdict, computed server-side:

| Colour    | Meaning                                              |
|-----------|------------------------------------------------------|
| `green`   | Within thresholds                                    |
| `yellow`  | Degraded — slow, or a low rate of errors             |
| `red`     | Problem — high error rate, or very slow              |
| `unknown` | No data. Deliberately **not** green: a status page   |
|           | that reports healthy when it knows nothing is worse  |
|           | than one that admits it                              |

`status.overall` is the worst of its parts, so it cannot be green while any
component is not.

Only the services in `PUBLIC_SERVICES` (`app.py`) are reported — currently
`wikibase`, `wdqs` and `ipfs`. Traefik routes around eighteen, the rest being
internal infrastructure (`ipfs-admin`, `wdqs-internal`, `mscbackend` and so
on); publishing all of them would put a map of the stack on a public
endpoint. Add a service to that dict to expose it; nothing else changes.

`status.detail` always states the cause in plain words ("Caused by slower
than usual response times"). A verdict on its own reads as arbitrary when
the service lights below it are all green, so the page renders this under
the headline.

Thresholds are the `*_WARN_*` / `*_CRIT_*` constants in `app.py`, each
overridable by environment variable. They are deliberately loose, and sit
**above** the portal's normal operating band rather than at some notion of
"good": p95 here routinely exceeds 1s and p99 exceeds 3s, so an initial
1.0s warn level left the page permanently amber. A status page stuck on a
warning gets ignored, and then nobody notices the day it means something.
Check the Grafana latency panel over a week before tightening these.

## What the public endpoint does *not* report

Per-service request and error rates, the CrowdSec block rate and the
portal-wide error percentage are all computed — they are what the traffic
lights are decided from — but not published. They describe internal
behaviour, and Grafana already holds the raw series for anyone who needs to
look closer. `public_view()` in `app.py` is the single place that decides
this.

`recent_edits` is withheld for a different reason: the same SPARQL query over
the same 7-day window has returned 120, 2,308 and 46,331 on different days.
Until that variance is explained it should not be presented as fact.

Response-time percentiles *are* published: a WMF user-facing metric,
aggregate, and they give a "degraded" verdict something concrete to stand on.

## Cross-origin access

`/api/status.json` sends `Access-Control-Allow-Origin` so a page hosted
elsewhere — the GitHub Pages site — can read it. Without that header the
request still succeeds but the browser refuses to hand the response to the
page, and the console error does not name CORS as the cause.

Default is `*`: the endpoint is public, read-only and carries no
credentials, so restricting origins protects nothing that `curl` could not
reach anyway, and it lets other consumers use the API without a config
change here.

**Do not set CORS at the reverse proxy as well.** Two
`Access-Control-Allow-Origin` headers on one response cause browsers to
reject it — a duplicated permissive header counts as no permission at all.
If the proxy is already configured to send them, set `CORS_ORIGIN=""` here.

## Endpoints

| Path                | Purpose                                    |
|---------------------|--------------------------------------------|
| `/`                 | The status page                            |
| `/api/status.json`  | All metrics as JSON                        |
| `/healthz`          | Liveness probe (no upstream dependencies)  |

## Configuration

| Variable                  | Default                                     |
|---------------------------|---------------------------------------------|
| `METRICS_BACKEND`         | `grafana` (or `prometheus`)                 |
| `GRAFANA_URL`             | `https://grafana-mardi.zib.de`              |
| `GRAFANA_TOKEN`           | *(required when backend is grafana)*        |
| `GRAFANA_DATASOURCE_UID`  | `prometheus`                                |
| `PROMETHEUS_URL`          | `https://prometheus-mardi.zib.de`           |
| `SPARQL_URL`              | `https://query.portal.mardi4nfdi.de/sparql` |
| `CACHE_TTL_SECONDS`       | `30`                                        |
| `QUERY_TIMEOUT_SECONDS`   | `10`                                        |
| `EDIT_WINDOW_DAYS`        | `7`                                         |
| `CORS_ORIGIN`             | `*` (empty disables the header)             |
| `TRAEFIK_ENTRYPOINT`      | `websecure`                                 |
| `TRAEFIK_JOB`             | `traefik`                                   |

`GRAFANA_TOKEN` is required and has no default: the service refuses to start
without it, rather than running permanently degraded in a way nobody notices.

## Getting a Grafana token

In Grafana: **Administration → Users and access → Service accounts → Add
service account**. Give it the **Viewer** role — that is enough to run
queries, and nothing more. Then **Add service account token** and copy the
value; Grafana shows it only once.

Viewer is the least privilege that works. Do not use an Admin token, and do
not reuse a personal API key: a service account can be revoked without
affecting anyone's login.

The token is a credential. Keep it out of the repository — pass it as an
environment variable, and in production as a Kubernetes Secret or Docker
secret rather than a literal in a compose file.

## Run locally

Requires the ZIB VPN, since Prometheus is not publicly reachable.

    pip install -r requirements.txt

    # PowerShell
    $env:GRAFANA_TOKEN = "glsa_..."
    python app.py

    # bash
    GRAFANA_TOKEN=glsa_... python app.py

    # http://localhost:8080

Requires the ZIB VPN, since Grafana is also behind it.

Without the VPN the page still loads and shows SPARQL metrics; the Traefik
cards render as unavailable. That degradation is deliberate.

To bypass Grafana and query Prometheus directly:

    $env:METRICS_BACKEND = "prometheus"
    python app.py

## Build and run the container

    docker build -t mardi-status .
    docker run -p 8080:8080 \
      -e GRAFANA_TOKEN=glsa_... \
      -e GRAFANA_URL=https://grafana-mardi.zib.de \
      mardi-status

## Deploying into the portal stack

Traefik labels, if deployed via Compose:

    labels:
      - traefik.enable=true
      - traefik.http.routers.status.rule=Host(`status.portal.mardi4nfdi.de`)
      - traefik.http.routers.status.entrypoints=websecure
      - traefik.http.routers.status.tls.certresolver=letsencrypt
      - traefik.http.services.status.loadbalancer.server.port=8080

The service needs to reach Grafana (or Prometheus, depending on backend), and
should be excluded from CrowdSec blocking rules so the page stays reachable
during an incident.

`GRAFANA_TOKEN` should come from a Kubernetes Secret, not an environment
literal in the manifest.

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

## Tests

    python test_grafana.py    # Grafana backend, against a fake Grafana
    python test_local.py      # direct Prometheus backend, against a fake one

Neither needs the VPN or a real token.

## Notes on the metrics

`request_rate` excludes HTTP 403. Those are CrowdSec bot blocks, and they have
run at several times the volume of all real traffic — including them would
make the headline number track bot waves rather than portal health. They are
reported separately as `blocked_rate`.

Per-service queries write the extracted service name into a **new** label
(`svc`) rather than overwriting `exported_service`. Overwriting it makes two
distinct series identical, and PromQL rejects the whole query with "vector
cannot contain metrics with the same labelset". This happens whenever two
Traefik entries reduce to the same name — overlapping deploy generations, or
one service registered by more than one provider.
