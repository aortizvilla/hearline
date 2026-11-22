# tracking/

- `costs.py`: prices every LLM and TTS call with the rates in `config/pricing.yaml` and saves it as a `CostEvent` row. It also totals today's spend, which the worker checks against the daily budget, and builds the cost report (`cost_report`: by stage, per episode, per active user).
- `kpis.py`: aggregates the data model into daily facts (`fact_kpi_daily`, `fact_listener_daily`, `fact_topic_daily`) for the dashboard, one day at a time.
- `dashboard.py`: the admin dashboard's numbers, read from those facts: audience, engagement, cost (per listener, per hour, per episode: shared and extra), time to generate an episode end to end (median and p95, by stage), top topics, pipeline health, with filters.
- `alerts.py`: alerts for whoever runs Hearline (a job failed for good, the daily budget cap, a failed daily plan): an `ALERT` log line, plus an optional webhook (Slack, Teams, Discord).
- `metrics.py`: simple in-memory counters, such as how often a clip was reused instead of generated, for the reports.

See the numbers with `python -m app.cli costs`, or the admin dashboard.
