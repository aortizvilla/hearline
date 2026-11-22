# jobs/

Decides when each pipeline step runs, and runs it safely.

- `scheduler.py`: the **daily plan** and the job handlers. The plan:
  1. counts active subscribers per topic;
  2. queues one research job per active topic;
  3. pre-generates clips for each topic's top stories;
  4. queues one episode job per user.

  Episodes are built in the 3 hours before the user's delivery time, at a fixed spot per user, so a whole timezone isn't built at 7:00 at once. They become visible at the delivery time. An episode waits only for **its own topics'** research, and stops waiting 15 minutes before delivery. Everything is inserted in bulk, so planning for 1M users takes a few thousand statements.
- `worker.py`: the **queue and worker**. Jobs are rows in the `ops_job` table. A worker claims one, runs it and marks it done. Failures retry with growing delays, and after too many attempts a job is marked `dead` instead of retrying forever. Each job has a unique key so it's never queued twice, and workers stop taking jobs once the daily budget is spent.
- `maintenance.py`: the **daily clean-up**, queued by the plan, so fast-growing tables stay small:
  - writes daily cost totals to `fact_cost_daily`, then deletes raw cost events older than 90 days;
  - moves heard-story rows older than 30 days to `storage/archive/`;
  - deletes finished jobs after 7 days and dead ones after 30.

Run a worker with `python -m app.jobs.worker`; run the clean-up by hand with `python -m app.cli maintenance`. All limits are in `config/pipeline.yaml` (`scheduler`, `maintenance`).
