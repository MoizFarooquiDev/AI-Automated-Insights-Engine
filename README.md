# CNI Insights Engine

Automated daily insights for solar and battery energy storage (BESS) sites. The pipeline pulls site data from Elasticsearch, runs rule-based detectors against configurable thresholds, and pushes the resulting insight records to Elasticsearch through AWS SQS.

## What it does

For every eligible site (`Contract.Type == 0`), the job:

1. Authenticates against the platform API and fetches the site list and site config.
2. Loads the insight mapping form (category / sub-category / detail-category, descriptions, severities) from DynamoDB via the platform API.
3. Fetches data from Elasticsearch:
   - **Processed data** for the last 7 days (`site-cni-processed-data`)
   - **Raw data** for yesterday (site's raw index)
   - **Forecast / processed data** for yesterday
4. Runs the **solar** and/or **battery** detectors, depending on the site's `site_source` types.
5. Combines the insights and ingests them into the `cni-insights-processed` index through SQS, as idempotent upserts keyed by a SHA-256 hash of site, date, and category codes.

## Detectors

Thresholds live in `thresholds.json`, so they can be tuned without touching code.

| Domain  | Detector                  | Code    |
|---------|---------------------------|---------|
| Solar   | Underperforming inverters | `0,1,2` |
| Solar   | Underperforming strings   | `0,1,3` |
| Solar   | Thermal derating          | `0,1,5` |
| Solar   | Inverter downtime         | `0,2,0` |
| Solar   | Uptime below commitment   | `2,1,2` |
| Battery | Low plant availability    | `0,3,0` |
| Battery | High cell temperature     | `5,3,0` |
| Battery | SOC imbalance             | `5,3,1` |
| Battery | High / low SOC            | `5,3,2`, `5,3,3` |
| Battery | High PCS temperature      | `5,3,4` |
| Battery | BESS full utilization     | `5,3,5` |
| Battery | Max / min cell voltage    | `5,3,7`, `5,3,8` |
| Battery | Max / min frequency       | `5,3,9`, `5,3,10` |
| Battery | High / low AC voltage     | `5,3,11`, `5,3,12` |

## Project structure

```
.
├── main.py              # Orchestrator: loops over sites, runs detectors, ingests results
├── apis.py              # Platform API: login, site list, site config, insight mapping form
├── data_fetching.py     # Elasticsearch scrolled queries -> pandas DataFrames
├── solar.py             # Solar insight detectors
├── battery.py           # Battery (BESS) insight detectors
├── injection.py         # Builds bulk upsert payloads and sends them to SQS
├── thresholds.py        # Loads thresholds.json (exposes SOLAR and BATTERY dicts)
├── thresholds.json      # Tunable detector thresholds
├── config.py            # Reads all settings/secrets from environment variables
├── .env.example         # Template for required environment variables
└── requirements.txt
```

## Tech stack

Python 3.10+, pandas, pvlib, requests, boto3, Elasticsearch (REST), AWS SQS, DynamoDB (via platform API).

## Setup

```bash
git clone <your-repo-url>
cd <your-repo>
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Configuration

Secrets are never stored in the code. Copy the template and fill in your values:

```bash
cp .env.example .env
```

| Variable | Purpose |
|----------|---------|
| `CNI_API_BASE_URL`, `CNI_API_KEY` | Platform API endpoint and key |
| `CNI_USERNAME`, `CNI_PASSWORD` | Platform login |
| `CNI_FORM_ID` | Insight mapping form ID |
| `ES_BASE_URL`, `ES_USER`, `ES_PASSWORD` | Elasticsearch access |
| `SQS_URL`, `AWS_REGION` | Ingestion queue |

`.env` is git-ignored. AWS credentials are picked up by `boto3` from the standard chain (env vars, `~/.aws`, or an IAM role), which needs `sqs:SendMessage` on the queue.

## Usage

```bash
python main.py
```

The run prints per-site progress (`[OK]`, `[SKIP]`, `[ERROR]`) and a final summary of eligible vs. skipped sites. Since the date windows are computed relative to today, schedule it once a day (cron, EventBridge, or Lambda).

## Tuning thresholds

Edit `thresholds.json`, for example:

```json
"high_cell_temp": { "_code": "5,3,0", "max_temp_c": 40 }
```

Detectors read these values through `thresholds.py`, so no code change is needed.

## Output schema

Each insight record written to `cni-insights-processed` contains:

`observationDate`, `siteId`, `siteName`, `category`, `subCategory`, `detailCategory`, `description`, `status`, `sev`, `opportunityCost`

Document IDs are deterministic, so re-running for the same day updates existing records instead of duplicating them.

## Notes

- Sites are skipped if their config is empty, `Contract.Type != 0`, or any data fetch fails.
- Solar detectors run only for sites with a `solar` source; battery detectors only for sites with a `bess` source.
- ES queries use the `Asia/Karachi` time zone.

## License

Add a license of your choice (e.g. MIT) or mark the repo private if it contains proprietary logic.
