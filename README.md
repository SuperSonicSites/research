# Supersonic Research

Reusable market-research environment for underwriting lead-generation niches with DataForSEO.

## What it does

- Expands seed keywords with DataForSEO Labs keyword ideas
- Pulls Google Ads search volume, CPC and competition
- Samples Google SERPs for commercial-intent keywords
- Aggregates niche-level demand and advertiser signals
- Saves raw API responses plus normalized CSV/JSON summaries
- Runs manually in GitHub Actions and uploads results as an artifact

## Required GitHub Secrets

Add these under **Settings → Secrets and variables → Actions**:

- `DATAFORSEO_LOGIN`
- `DATAFORSEO_PASSWORD`

Do not commit credentials to this repository.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export DATAFORSEO_LOGIN="..."
export DATAFORSEO_PASSWORD="..."
python research.py --config configs/farmer_market.json
```

## Run in GitHub Actions

Open **Actions → DataForSEO Market Research → Run workflow** and supply the config path. Results are uploaded as `market-research-results`.

## Output

Each run creates a timestamped directory under `outputs/` containing:

- `keyword_metrics.csv`
- `niche_summary.csv`
- `serp_samples.json`
- `raw/` API responses
- `report.md`

## Current first market

`configs/farmer_market.json` contains the farmer-facing verticals we are underwriting for large $10K–$100K+/month lead buyers.
