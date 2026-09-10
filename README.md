# Tender Radar UK & Ireland

A live, automatically refreshed dashboard for technology procurement opportunities across:

- Find a Tender (UK)
- Sell2Wales
- Public Contracts Scotland
- eTenders Ireland / TED

The monitor focuses on cloud, SaaS, software, websites, digital platforms, AI, hosting, cyber security, data, CRM/ERP, managed IT, integrations and wider ICT systems.

## How it works

1. `scripts/fetch_tenders.py` reads public/open procurement feeds.
2. Opportunities are normalised, deduplicated and scored for technology relevance.
3. `data/tenders.json` is refreshed by GitHub Actions every 15 minutes.
4. The static dashboard loads the JSON and rechecks it every 2 minutes in the browser.
5. GitHub Pages can publish the dashboard directly from this repository.

No API keys are required for the initial sources.

## Data sources

### Find a Tender
Uses the official OCDS release-package API and Open Government Licence data.

### Sell2Wales
Uses the official Sell2Wales OCDS notices API.

### Public Contracts Scotland
Uses the official Public Contracts Scotland OCDS notices API.

### eTenders Ireland
The live Irish eTenders portal does not expose a conventional public live API for all notices. The monitor therefore uses the official TED Search API for active Irish notices that are published to TED. The source adapter is isolated so direct eTenders below-threshold ingestion can be added later if a stable permitted machine-readable endpoint becomes available.

## Local run

```bash
python -m pip install -r requirements.txt
python scripts/fetch_tenders.py
python -m http.server 8000
```

Then open `http://localhost:8000`.

## GitHub Pages

The repository includes `.github/workflows/pages.yml`. In GitHub go to **Settings → Pages → Build and deployment → Source → GitHub Actions**. After that, pushes to `main` deploy automatically.

## Automated refresh

`.github/workflows/refresh.yml` runs every 15 minutes and can also be triggered manually from the Actions tab. It commits changed public tender data back to `data/tenders.json`. The Pages workflow then publishes the updated site.

## Filters

The scoring model combines technology CPV prefixes with keyword matching. Edit `TECH_KEYWORDS`, `STRONG_KEYWORDS` and `TECH_CPV_PREFIXES` in `scripts/fetch_tenders.py` to tune the feed.

## Notes

This is a monitoring and discovery tool. Always verify the deadline, eligibility, value and tender documents on the original contracting portal before bidding.
