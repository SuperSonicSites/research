#!/usr/bin/env python3
import argparse, csv, json, math, os, re, statistics, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import requests

BASE_URL = "https://api.dataforseo.com/v3"

class DFS:
    def __init__(self, login, password, raw_dir):
        self.auth = (login, password)
        self.raw = Path(raw_dir)
        self.raw.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.total_cost = 0.0

    def post(self, path, payload, label, retries=4):
        url = f"{BASE_URL}/{path.lstrip('/')}"
        last_error = None
        for attempt in range(retries):
            r = self.session.post(url, auth=self.auth, json=payload, timeout=180)
            r.raise_for_status()
            data = r.json()
            (self.raw / f"{label}_attempt{attempt+1}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
            if data.get("status_code") != 20000:
                last_error = RuntimeError(f"API {data.get('status_code')} {data.get('status_message')}")
            else:
                tasks = data.get("tasks") or []
                bad = next((t for t in tasks if t.get("status_code") != 20000), None)
                if not bad:
                    for t in tasks:
                        self.total_cost += float(t.get("cost") or 0)
                    return [t.get("result") or [] for t in tasks]
                last_error = RuntimeError(f"Task {bad.get('status_code')} {bad.get('status_message')}")
                if bad.get("status_code") not in {40202, 40203, 40204}:
                    raise last_error
            if attempt < retries - 1:
                time.sleep(65 * (attempt + 1))
        raise last_error

def num(v):
    try: return float(v or 0)
    except (TypeError, ValueError): return 0.0

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)

def extract_suggestion_keywords(task_results):
    out = []
    for result in task_results:
        for block in result:
            for item in block.get("items") or []:
                if item.get("keyword"):
                    out.append(item["keyword"].lower().strip())
    return out

def extract_overview(task_results):
    rows = []
    for result in task_results:
        for block in result:
            for item in block.get("items") or []:
                kw = (item.get("keyword") or "").lower().strip()
                if not kw: continue
                info = item.get("keyword_info") or {}
                props = item.get("keyword_properties") or {}
                intent = item.get("search_intent_info") or {}
                serp = item.get("serp_info") or {}
                rows.append({
                    "keyword": kw,
                    "search_volume": info.get("search_volume"),
                    "cpc": info.get("cpc"),
                    "competition": info.get("competition"),
                    "competition_level": info.get("competition_level"),
                    "low_top_of_page_bid": info.get("low_top_of_page_bid"),
                    "high_top_of_page_bid": info.get("high_top_of_page_bid"),
                    "monthly_searches": info.get("monthly_searches") or [],
                    "main_intent": intent.get("main_intent"),
                    "foreign_intent": intent.get("foreign_intent") or [],
                    "core_keyword": props.get("core_keyword"),
                    "keyword_difficulty": props.get("keyword_difficulty"),
                    "serp_item_types": serp.get("serp_item_types") or [],
                })
    return rows

def weighted_avg(rows, field):
    den = sum(num(r.get("search_volume")) for r in rows)
    return sum(num(r.get("search_volume")) * num(r.get(field)) for r in rows) / den if den else 0

def deduped_sv(rows):
    groups = defaultdict(list)
    for r in rows:
        core = (r.get("core_keyword") or r.get("keyword") or "").lower().strip()
        groups[core].append(num(r.get("search_volume")))
    return round(sum(max(vs) for vs in groups.values() if vs))

def pct_rank_log(values, x):
    vals = sorted(math.log1p(max(0, v)) for v in values)
    if not vals: return 0
    lx = math.log1p(max(0, x))
    return 100 * sum(v <= lx for v in vals) / len(vals)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--output-root", default="outputs")
    args = p.parse_args()

    login, password = os.getenv("DATAFORSEO_LOGIN"), os.getenv("DATAFORSEO_PASSWORD")
    if not login or not password: raise SystemExit("DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD required")
    cfg = json.load(open(args.config, encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.output_root) / f"{cfg['market_name'].lower().replace(' ', '_')}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    api = DFS(login, password, out / "raw")
    loc, lang = cfg.get("location_code", 2840), cfg.get("language_code", "en")
    suggestion_limit = int(cfg.get("max_suggestions_per_seed", 60))
    global_ex = re.compile(cfg.get("global_exclude_regex", "a^"), re.I)
    global_brand = re.compile(cfg.get("global_brand_regex", "a^"), re.I)

    keyword_rows, rejected_rows, summaries = [], [], []
    for idx, niche in enumerate(cfg["niches"], 1):
        name = niche["name"]
        seeds = [s.lower().strip() for s in niche["seeds"]]
        include = re.compile(niche["include_regex"], re.I)
        exclude = re.compile(niche.get("exclude_regex", "a^"), re.I)
        brand = re.compile(niche.get("brand_regex", cfg.get("global_brand_regex", "a^")), re.I)
        print(f"[{idx}/{len(cfg['niches'])}] {name}", flush=True)

        pool = set(seeds)
        for j, seed in enumerate(seeds, 1):
            payload = [{
                "keyword": seed, "location_code": loc, "language_code": lang,
                "limit": suggestion_limit, "include_seed_keyword": True,
                "exact_match": True, "ignore_synonyms": False,
                "filters": ["keyword_info.search_volume", ">", 0]
            }]
            pool.update(extract_suggestion_keywords(api.post(
                "dataforseo_labs/google/keyword_suggestions/live", payload,
                f"{idx:02d}_{name}_suggestions_{j:02d}"
            )))

        qualified = []
        for kw in sorted(pool):
            if include.search(kw) and not global_ex.search(kw) and not exclude.search(kw):
                qualified.append(kw)
            elif kw not in seeds:
                rejected_rows.append({"niche": name, "keyword": kw, "reason": "relevance_filter"})
        qualified = sorted(set(qualified) | set(seeds))

        metrics = []
        for b in range(0, len(qualified), 700):
            payload = [{
                "keywords": qualified[b:b+700], "location_code": loc,
                "language_code": lang, "include_serp_info": True, "tag": name
            }]
            metrics.extend(extract_overview(api.post(
                "dataforseo_labs/google/keyword_overview/live", payload,
                f"{idx:02d}_{name}_overview_{b//700+1}"
            )))
        bykw = {r["keyword"]: r for r in metrics}

        rows = []
        for kw in qualified:
            r = dict(bykw.get(kw, {"keyword": kw, "search_volume": 0, "cpc": 0,
                "competition": 0, "competition_level": None, "low_top_of_page_bid": 0,
                "high_top_of_page_bid": 0, "monthly_searches": [], "main_intent": None,
                "foreign_intent": [], "core_keyword": None, "keyword_difficulty": None,
                "serp_item_types": []}))
            r.update({
                "niche": name, "label": niche.get("label", name), "category": niche.get("category", ""),
                "is_seed": kw in seeds, "is_branded": bool(brand.search(kw) or global_brand.search(kw)),
                "has_paid_serp": "paid" in (r.get("serp_item_types") or [])
            })
            rows.append(r); keyword_rows.append(r)

        nonzero = [r for r in rows if num(r["search_volume"]) > 0]
        commercial = [r for r in nonzero if r.get("main_intent") in {"commercial", "transactional"}]
        generic_commercial = [r for r in commercial if not r["is_branded"]]
        branded_commercial = [r for r in commercial if r["is_branded"]]
        seed_nonzero = sum(1 for s in seeds if num(bykw.get(s, {}).get("search_volume")) > 0)
        intent_coverage = sum(1 for r in nonzero if r.get("main_intent")) / len(nonzero) if nonzero else 0
        paid_presence = sum(1 for r in generic_commercial if r.get("has_paid_serp")) / len(generic_commercial) if generic_commercial else 0
        comp_vals = [num(r.get("competition")) * 100 for r in generic_commercial if r.get("competition") is not None]

        summaries.append({
            "niche": name, "label": niche.get("label", name), "category": niche.get("category", ""),
            "seed_count": len(seeds), "seed_nonzero_count": seed_nonzero,
            "qualified_keyword_count": len(rows), "nonzero_keyword_count": len(nonzero),
            "raw_qualified_sv": round(sum(num(r["search_volume"]) for r in nonzero)),
            "deduped_qualified_sv": deduped_sv(nonzero),
            "deduped_commercial_sv": deduped_sv(commercial),
            "deduped_generic_commercial_sv": deduped_sv(generic_commercial),
            "deduped_branded_commercial_sv": deduped_sv(branded_commercial),
            "generic_commercial_cpc": round(weighted_avg(generic_commercial, "cpc"), 2),
            "max_generic_high_bid": round(max([num(r["high_top_of_page_bid"]) for r in generic_commercial] or [0]), 2),
            "avg_paid_competition_index": round(statistics.mean(comp_vals), 1) if comp_vals else 0,
            "paid_serp_presence_share": round(paid_presence, 4),
            "native_intent_coverage": round(intent_coverage, 4),
        })

    svs = [r["deduped_generic_commercial_sv"] for r in summaries]
    cpcs = [r["generic_commercial_cpc"] for r in summaries]
    bids = [r["max_generic_high_bid"] for r in summaries]
    for r in summaries:
        demand = pct_rank_log(svs, r["deduped_generic_commercial_sv"])
        cpc = pct_rank_log(cpcs, r["generic_commercial_cpc"])
        bid = pct_rank_log(bids, r["max_generic_high_bid"])
        comp = min(100, r["avg_paid_competition_index"])
        paid = min(100, r["paid_serp_presence_share"] * 100)
        r["search_opportunity_score"] = round(.40*demand + .25*cpc + .15*bid + .10*comp + .10*paid, 1)
        quality = 100
        if r["seed_nonzero_count"] / max(1, r["seed_count"]) < .5: quality -= 20
        if r["nonzero_keyword_count"] < 5: quality -= 15
        if r["native_intent_coverage"] < .8: quality -= 20
        if r["deduped_qualified_sv"] == 0: quality -= 40
        r["research_quality_score"] = max(0, quality)
        r["confidence"] = "A" if quality >= 90 else "B" if quality >= 75 else "C" if quality >= 55 else "D"
        r["search_verdict"] = "GO" if r["search_opportunity_score"] >= 72 and quality >= 75 else "TEST" if r["search_opportunity_score"] >= 55 and quality >= 55 else "NO-GO"
    summaries.sort(key=lambda r: r["search_opportunity_score"], reverse=True)

    fields = ["niche","label","category","keyword","is_seed","is_branded","main_intent","foreign_intent","search_volume","cpc","competition","competition_level","low_top_of_page_bid","high_top_of_page_bid","core_keyword","keyword_difficulty","has_paid_serp","monthly_searches","serp_item_types"]
    flat = []
    for r in keyword_rows:
        x = dict(r)
        for k in ["foreign_intent", "monthly_searches", "serp_item_types"]: x[k] = json.dumps(x.get(k) or [])
        flat.append({k: x.get(k) for k in fields})
    write_csv(out / "keyword_metrics.csv", flat, fields)
    write_csv(out / "niche_summary.csv", summaries, list(summaries[0].keys()))
    write_csv(out / "rejected_keywords.csv", rejected_rows, ["niche","keyword","reason"])

    evals = [
        {"name":"all_niches_return_metrics", "pass": all(r["qualified_keyword_count"] > 0 for r in summaries)},
        {"name":"90pct_niches_have_nonzero_demand", "pass": sum(r["deduped_qualified_sv"] > 0 for r in summaries) >= math.ceil(len(summaries)*.9)},
        {"name":"80pct_seed_coverage", "pass": sum(r["seed_nonzero_count"] for r in summaries) / max(1,sum(r["seed_count"] for r in summaries)) >= .8},
        {"name":"80pct_native_intent_coverage", "pass": sum(1 for r in keyword_rows if num(r["search_volume"]) > 0 and r.get("main_intent")) / max(1,sum(1 for r in keyword_rows if num(r["search_volume"]) > 0)) >= .8},
        {"name":"no_absurd_5m_deduped_niche", "pass": max(r["deduped_qualified_sv"] for r in summaries) < 5_000_000},
    ]
    eval_doc = {"generated_at": datetime.now(timezone.utc).isoformat(), "passes": sum(e["pass"] for e in evals), "total": len(evals), "all_pass": all(e["pass"] for e in evals), "evals": evals, "api_reported_cost_estimate": round(api.total_cost,4)}
    (out / "eval.json").write_text(json.dumps(eval_doc, indent=2), encoding="utf-8")

    lines = [f"# {cfg['market_name']} — Farm Lead-Gen Search Underwriting", "", f"Generated: {datetime.now(timezone.utc).isoformat()}", "", f"Automated evals: **{eval_doc['passes']}/{eval_doc['total']} passed**.", "", "Search volume below is deduped by DataForSEO core-keyword clusters. The primary demand metric excludes branded/OEM queries and counts only native commercial or transactional intent.", "", "| Rank | Niche | Verdict | Score | Generic commercial SV | Branded commercial SV | CPC | Max bid | Paid SERP | Confidence |", "|---:|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for i,r in enumerate(summaries,1):
        lines.append(f"| {i} | {r['label']} | {r['search_verdict']} | {r['search_opportunity_score']:.1f} | {r['deduped_generic_commercial_sv']:,} | {r['deduped_branded_commercial_sv']:,} | ${r['generic_commercial_cpc']:.2f} | ${r['max_generic_high_bid']:.2f} | {r['paid_serp_presence_share']:.0%} | {r['confidence']} |")
    lines += ["", "## Top generic commercial terms", ""]
    for s in summaries[:15]:
        lines.append(f"### {s['label']}")
        top = sorted([r for r in keyword_rows if r["niche"]==s["niche"] and not r["is_branded"] and r.get("main_intent") in {"commercial","transactional"}], key=lambda r:(num(r["search_volume"]),num(r["cpc"])), reverse=True)[:10]
        lines.append(", ".join(f"{r['keyword']} ({int(num(r['search_volume'])):,}/mo, ${num(r['cpc']):.2f})" for r in top) or "No generic commercial terms with measurable demand.")
        lines.append("")
    lines += ["## Decision rule", "", "This is the **search-side** verdict only. Final GO requires enterprise buyer proof: credible $10K–$100K/month capacity, accepted lead criteria, geography, price, and evidence that third-party lead acquisition fits the buyer's channel model."]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Results written to {out}; evals {eval_doc['passes']}/{eval_doc['total']}; API cost ${api.total_cost:.4f}", flush=True)

if __name__ == "__main__": main()
