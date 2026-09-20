# Plan: run the collector on AWS Lambda (EventBridge Scheduler)

> **Status: deferred alternative (not the current setup).**
> The reliability problem was solved more simply with cron-job.org calling GitHub `workflow_dispatch` (see `HANDOFF_COLLECTION_RELIABILITY.md`).
> Keep this as the upgrade path if cron-job.org proves insufficient, or if you want 24/7 self-owned scheduling without a third party holding a token.

Replace the unreliable GitHub Actions `schedule` with a Lambda that runs
`scout collect` directly, triggered by EventBridge Scheduler. This removes GitHub
from the collection path entirely — no `workflow_dispatch`, no GitHub PAT — and keeps
all secrets inside your own AWS account.

Context for the problem this solves: `HANDOFF_COLLECTION_RELIABILITY.md`.

---

## 1. Architecture

```
EventBridge Scheduler  (reliable managed cron, every 15 min)
        │  invokes
        ▼
AWS Lambda  (container image: twitch_scout + deps)
        │  lambda_handler.handler → Collector.run()
        ├── Twitch Helix API   (App Access Token)
        └── Turso (libSQL)      (write snapshots)
```

The collector already self-decides tier from a Pacific-time clock and is idempotent
(skips an already-sampled slot **before** any API call), so a fixed 15-min trigger is
safe and cheap: most invocations during an hour find the baseline slot already
written and return in milliseconds. Real work happens ~hourly (baseline) and
~per-10-min during the Tue/Thu/Sat evening windows.

## 2. Why Lambda fits

- The tool is pure Python + Turso over HTTP (`turso_serverless`) — no persistent DB
  connection, no local disk needed. Ideal for a stateless function.
- A run is ~1–3 min (top 500 games); Lambda's 15-min ceiling is ample.
- Secrets live in Lambda config / SSM, in your account. No third party.
- EventBridge Scheduler is enterprise-grade reliable, unlike GitHub `schedule`.

## 3. Components to create

### 3.1 Lambda handler (`lambda_handler.py`, repo root)

Reuses the existing code; no logic duplicated.

```python
import logging

from twitch_scout.clock import SystemClock
from twitch_scout.collect.collector import Collector
from twitch_scout.config import Config
from twitch_scout.store.db import connect
from twitch_scout.twitch.client import HelixClient

logging.getLogger().setLevel(logging.INFO)


def handler(event, context):  # noqa: ANN001, ARG001 -- Lambda signature
    config = Config.from_env()
    creds = config.require_twitch()
    conn = connect(config.db, auth_token=config.turso_auth_token)
    try:
        with HelixClient.create(creds.client_id, creds.client_secret) as client:
            result = Collector(client, conn, SystemClock(), config=config.collector).run()
    finally:
        conn.close()
    return {
        "tier": str(result.slot.tier),
        "ts": result.slot.ts.isoformat(),
        "skipped": result.skipped,
        "written": result.games_written,
        "failed": result.games_failed,
    }
```

### 3.2 Container image (`Dockerfile`, repo root)

pydantic-core ships a compiled wheel, so a container image is the clean packaging
path (vs a zip + layer).

```dockerfile
FROM public.ecr.aws/lambda/python:3.12
COPY pyproject.toml ${LAMBDA_TASK_ROOT}/
COPY twitch_scout ${LAMBDA_TASK_ROOT}/twitch_scout
COPY lambda_handler.py ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir ".[turso]"
CMD ["lambda_handler.handler"]
```

### 3.3 Secrets / config (Lambda environment variables)

Set on the function (encrypted at rest with the default KMS key):

- `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
- `SCOUT_DB` (the `libsql://…` Turso URL)
- `TURSO_AUTH_TOKEN`
- optional: `SCOUT_TOP_N`, `SCOUT_STREAMS_MAX_PAGES`

More secure alternative: store `TWITCH_CLIENT_SECRET` and `TURSO_AUTH_TOKEN` in **SSM
Parameter Store (SecureString)** and read them at cold start (adds `ssm:GetParameter`
+ `kms:Decrypt` to the role). Env vars are acceptable for a single-user tool; SSM is
the upgrade if you want them out of the function config.

### 3.4 IAM

- **Lambda execution role**: `AWSLambdaBasicExecutionRole` (CloudWatch Logs). Add SSM
  read + KMS decrypt only if using 3.3's SSM option.
- **Scheduler role**: trust `scheduler.amazonaws.com`; permission
  `lambda:InvokeFunction` on this function.

### 3.5 EventBridge Scheduler

- Schedule expression: `rate(15 minutes)` (timezone-agnostic; the collector's PT
  clock decides tier). Flexible time window: off.
- Target: the Lambda function, with the scheduler role above.
- Optional economy: two schedules instead of one — hourly always + every 10 min
  during the UTC hours that hold the PT window — but the idempotent skip already makes
  the flat 15-min schedule cheap, so start simple.

## 4. Deployment steps

1. Add `lambda_handler.py` and `Dockerfile` to the repo (section 3).
2. Build and push the image to ECR:
   ```bash
   aws ecr create-repository --repository-name twitch-scout
   aws ecr get-login-password --region <region> | docker login --username AWS \
     --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
   docker build -t twitch-scout .
   docker tag twitch-scout:latest <acct>.dkr.ecr.<region>.amazonaws.com/twitch-scout:latest
   docker push <acct>.dkr.ecr.<region>.amazonaws.com/twitch-scout:latest
   ```
3. Create the Lambda from the image: memory ~512 MB, timeout 300 s (5 min), env vars
   from 3.3, the execution role from 3.4.
4. Test: invoke once (console "Test", empty event) → expect a JSON result with
   `written` > 0 (or `skipped: true` if that slot already exists) and a new Turso row
   count.
5. Create the EventBridge Scheduler schedule (3.5) targeting the function.
6. Watch CloudWatch Logs for a couple of cycles; confirm window-tier rows appear in
   Turso on the next Tue/Thu/Sat evening.

## 5. Retire the GitHub scheduler

Once Lambda is verified for a full stream night:

- Remove the `schedule:` triggers from `.github/workflows/collect.yml` (keep
  `workflow_dispatch` for manual/backfill runs, or delete the workflow entirely).
- Drop the `turso` extra install note from CI if the workflow is removed.

Keep the `scout` CLI and all library code unchanged — Lambda and the CLI share it.

## 6. Cost (order of magnitude, this workload)

- Lambda: ~2,880 invocations/mo, most sub-second (idempotent skips); real runs
  ~1–3 min. Well within the free tier (1M req, 400k GB-s).
- EventBridge Scheduler: free at this volume.
- ECR: ~$0.10/mo for the image.
- SSM Parameter Store (standard): free. (Secrets Manager, if used instead: ~$0.40/mo
  per secret.)

Effectively ~$0/mo.

## 7. Coding-standards notes

- The collector already has bounded loops, per-call timeouts, per-game failure
  isolation, and idempotent writes — the handler just wires it up.
- The handler must not swallow errors: let exceptions propagate so the invocation is
  marked failed and shows in CloudWatch (a failed sample is logged, not hidden).
- Keep the clock injected (`SystemClock`) as today; tier logic stays testable.

## 8. Open decisions

- Env vars vs SSM for the two real secrets (start: env vars; upgrade: SSM).
- Flat `rate(15 minutes)` vs two window-aware schedules (start: flat).
- Manual console/CLI deploy now vs IaC (SAM/Terraform) later — start manual, capture
  as IaC once it's stable.
- Region — pick one near the Turso db region (`aws-us-west-2`) to shave latency.
