#!/usr/bin/env python3
import argparse
import csv
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://api.dataforseo.com/v3"


class DataForSEO:
    def __init__(self, login, password, raw_dir):
        self.auth = (login, password)
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()

    def post(self, path, payload, label):
        url = f"{BASE_URL}/{path.lstrip('/')}"
        response = self.session.post(url, auth=self.auth, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        (self.raw_dir / f"{label}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        if data.get("status_code") != 20000:
            raise RuntimeError(f"DataForSEO request failed: {data.get('status_code')} {data.get('status_message')}")
        tasks = data.get("tasks") or []
        if not tasks:
            return []
        task = tasks[0]
        if task.get("status_code") != 20000:
            raise RuntimeError(f"DataForSEO task failed: {task.get('status_code')} {task.get('status_message')}")
        return task.get("result") or []


def safe_num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def extract_idea_items(result):
    items = []
    for block in result:
        for item in block.get("items") or []:
            kw = item.get("keyword")
            if not kw:
                continue
            info = ((item.get("keyword_data") or {}).get("keyword_info") or {})
            items.append({
                "keyword": kw,
                "search_volume": info.get("search_volume"),
                "cpc": info.get("cpc"),
                "competition_level": info.get("competition_level"),
            })
    return items


def extract_search_volume(result):
    rows = []
    for item in result:
        if not isinstance(item, dict) or not item.get("keyword"):
            continue
        rows.append({
            "keyword": item.get("keyword"),
            "search_volume": item.get("search_volume"),
            "cpc": item.get("cpc"),
            "competition": item.get("competition"),
            "competition_index": item.get("competition_index"),
            "low_top_of_page_bid": item.get("low_top_of_page_bid"),
            "high_top_of_page_bid": item.get("high_top_of_page_bid"),
            "monthly_searches": item.get("monthly_searches") or [],
        })
    return rows


def extract_serp(result):
    out = {"paid_domains": [], "organic_domains": [], "item_types": Counter()}
    for block in result:
        for item in block.get("items") or []:
            typ = item.get("type") or "unknown"
            out["item_types"][typ] += 1
            domain = item.get("domain")
            if typ in {"paid", "shopping"} and domain:
                out["paid_domains"].append(domain)
            elif typ == "organic" and domain:
                out["organic_domains"].append(domain)
    out["item_types"] = dict(out["item_types"])
    out["paid_domains"] = list(dict.fromkeys(out["paid_domains"]))
    out["organic_domains"] = list(dict.fromkeys(out["organic_domains"]))
    return out


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i+n]


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-root", default="outputs")
    args = parser.parse_args()

    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        raise SystemExit("DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD are required.")

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.output_root) / f"{config['market_name'].lower().replace(' ', '_')}_{stamp}"
    raw = out / "raw"
    out.mkdir(parents=True, exist_ok=True)
    api = DataForSEO(login, password, raw)

    location = config.get("location_code", 2840)
    language = config.get("language_code", "en")
    idea_limit = int(config.get("max_keyword_ideas_per_niche", 100))
    serp_n = int(config.get("serp_sample_size", 10))

    all_keyword_rows = []
    serp_samples = []
    summary_rows = []

    for niche_index, niche in enumerate(config["niches"], 1):
        name = niche["name"]
        seeds = list(dict.fromkeys(niche["seeds"]))
        print(f"[{niche_index}/{len(config['niches'])}] {name}")

        # DataForSEO Labs expands the seed set into relevant commercial-search territory.
        idea_payload = [{
            "keywords": seeds,
            "location_code": location,
            "language_code": language,
            "limit": idea_limit,
        }]
        idea_result = api.post(
            "dataforseo_labs/google/keyword_ideas/live",
            idea_payload,
            f"{niche_index:02d}_{name}_ideas",
        )
        ideas = extract_idea_items(idea_result)
        keywords = list(dict.fromkeys(seeds + [x["keyword"] for x in ideas]))

        metrics = []
        # Google Ads Live accepts up to 1000 keywords per request and has a 12 req/min account limit.
        for batch_index, batch in enumerate(chunks(keywords, 1000), 1):
            metrics_payload = [{
                "keywords": batch,
                "location_code": location,
                "language_code": language,
                "include_adult_keywords": False,
                "sort_by": "search_volume",
                "tag": name,
            }]
            res = api.post(
                "keywords_data/google_ads/search_volume/live",
                metrics_payload,
                f"{niche_index:02d}_{name}_volume_{batch_index}",
            )
            metrics.extend(extract_search_volume(res))
            time.sleep(5.2)

        for row in metrics:
            row["niche"] = name
            row["is_seed"] = row["keyword"] in seeds
            all_keyword_rows.append(row)

        ranked = sorted(metrics, key=lambda x: (safe_num(x["search_volume"]), safe_num(x["cpc"])), reverse=True)
        serp_targets = [r for r in ranked if safe_num(r["search_volume"]) > 0][:serp_n]
        paid_domains = Counter()
        for serp_index, metric in enumerate(serp_targets, 1):
            kw = metric["keyword"]
            serp_payload = [{
                "keyword": kw,
                "location_code": location,
                "language_code": language,
                "depth": 20,
                "tag": name,
            }]
            serp_result = api.post(
                "serp/google/organic/live/advanced",
                serp_payload,
                f"{niche_index:02d}_{name}_serp_{serp_index:02d}",
            )
            parsed = extract_serp(serp_result)
            for domain in parsed["paid_domains"]:
                paid_domains[domain] += 1
            serp_samples.append({
                "niche": name,
                "keyword": kw,
                "search_volume": metric.get("search_volume"),
                "cpc": metric.get("cpc"),
                **parsed,
            })

        nonzero = [r for r in metrics if safe_num(r["search_volume"]) > 0]
        total_sv = sum(safe_num(r["search_volume"]) for r in nonzero)
        weighted_cpc_num = sum(safe_num(r["search_volume"]) * safe_num(r["cpc"]) for r in nonzero)
        avg_cpc = weighted_cpc_num / total_sv if total_sv else 0
        high_bid = max([safe_num(r["high_top_of_page_bid"]) for r in nonzero] or [0])
        top_ads = ", ".join([d for d, _ in paid_domains.most_common(8)])

        summary_rows.append({
            "niche": name,
            "keyword_count": len(metrics),
            "nonzero_keyword_count": len(nonzero),
            "aggregate_monthly_search_volume": round(total_sv),
            "search_volume_weighted_cpc": round(avg_cpc, 2),
            "max_high_top_of_page_bid": round(high_bid, 2),
            "serp_keywords_sampled": len(serp_targets),
            "unique_paid_domains_seen": len(paid_domains),
            "top_paid_domains": top_ads,
        })

    keyword_fields = [
        "niche", "keyword", "is_seed", "search_volume", "cpc", "competition",
        "competition_index", "low_top_of_page_bid", "high_top_of_page_bid", "monthly_searches"
    ]
    flat_keyword_rows = []
    for r in all_keyword_rows:
        r = dict(r)
        r["monthly_searches"] = json.dumps(r.get("monthly_searches") or [])
        flat_keyword_rows.append(r)
    write_csv(out / "keyword_metrics.csv", flat_keyword_rows, keyword_fields)

    summary_fields = [
        "niche", "keyword_count", "nonzero_keyword_count", "aggregate_monthly_search_volume",
        "search_volume_weighted_cpc", "max_high_top_of_page_bid", "serp_keywords_sampled",
        "unique_paid_domains_seen", "top_paid_domains"
    ]
    summary_rows.sort(key=lambda r: (r["aggregate_monthly_search_volume"], r["search_volume_weighted_cpc"]), reverse=True)
    write_csv(out / "niche_summary.csv", summary_rows, summary_fields)
    (out / "serp_samples.json").write_text(json.dumps(serp_samples, indent=2), encoding="utf-8")

    report = [
        f"# {config['market_name']} — DataForSEO Market Research",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "This is a demand/competition layer, not a final go/no-go. Buyer capacity, lead price, regulations and close economics must be validated separately.",
        "",
        "| Niche | Aggregate monthly SV* | Weighted CPC | Max high bid | Paid domains seen |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in summary_rows:
        report.append(
            f"| {r['niche']} | {r['aggregate_monthly_search_volume']:,} | ${r['search_volume_weighted_cpc']:.2f} | ${r['max_high_top_of_page_bid']:.2f} | {r['unique_paid_domains_seen']} |"
        )
    report += [
        "",
        "\\* Aggregate search volume can double-count close variants and adjacent intents. Use it comparatively, not as exact TAM.",
        "",
        "## Next underwriting step",
        "",
        "For the leading niches, identify real $10K–$100K/month-capable buyers and validate accepted-lead criteria, price, geography and monthly cap.",
    ]
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"Results written to {out}")


if __name__ == "__main__":
    main()
