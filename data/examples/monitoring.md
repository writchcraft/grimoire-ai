# Example monitoring stack

service: grafana

This is fictional demonstration infrastructure. All addresses are documentation addresses.

## Endpoints

Grafana runs on monitoring.lab.example at 192.0.2.20, TCP port 3000.
Prometheus uses TCP port 9090 on the same example host.
Grafana URL: http://192.0.2.20:3000

## Dependencies

Grafana reads metrics from Prometheus. Its configuration is mounted read-only.
Prometheus scrapes node-exporter on TCP port 9100.

## Troubleshooting

For a blank dashboard, first check the configured Prometheus datasource URL, then
inspect the Prometheus targets page for failed scrapes. A failed scrape does not
by itself establish the root cause: check DNS, reachability and the target logs.
