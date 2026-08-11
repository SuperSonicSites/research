#!/usr/bin/env python3
import argparse, csv, json, math, os, re, statistics, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import requests
BASE_URL='https://api.dataforseo.com/v3'
class DFS:
    def __init__(self,login,password,raw_dir):
        self.auth=(login,password); self.raw=Path(raw_dir); self.raw.mkdir(parents=True,exist_ok=True); self.s=requests.Session(); self.total_cost=0.0
    def post(self,path,payload,label):
        r=self.s.post(f"{BASE_URL}/{path.lstrip('/')}",auth=self.auth,json=payload,timeout=180); r.raise_for_status(); d=r.json(); (self.raw/f'{label}.json').write_text(json.dumps(d,indent=2),encoding='utf-8')
        if d.get('status_code')!=20000: raise RuntimeError(f"API {d.get('status_code')} {d.get('status_message')}")
        out=[]
        for t in d.get('tasks') or []:
            self.total_cost += float(t.get('cost') or 0)
            if t.get('status_code')!=20000: raise RuntimeError(f"Task {t.get('status_code')} {t.get('status_message')}")
            out.append(t.get('result') or [])
        return out
def num(x):
    try:return float(x or 0)
    except:return 0.0
def write_csv(path,rows,fields):
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
def extract_suggestions(task_results):
    out=[]
    for result in task_results:
        for block in result:
            for item in block.get('items') or []:
                kw=item.get('keyword')
                if not kw: continue
                kd=item.get('keyword_data') or {}; info=kd.get('keyword_info') or item.get('keyword_info') or {}
                out.append({'keyword':kw.lower().strip(),'search_volume':info.get('search_volume'),'cpc':info.get('cpc')})
    return out
def extract_volume(task_results):
    rows=[]
    for result in task_results:
        for item in result:
            if not isinstance(item,dict) or not item.get('keyword'): continue
            rows.append({'keyword':item['keyword'].lower().strip(),'search_volume':item.get('search_volume'),'cpc':item.get('cpc'),'competition':item.get('competition'),'competition_index':item.get('competition_index'),'low_top_of_page_bid':item.get('low_top_of_page_bid'),'high_top_of_page_bid':item.get('high_top_of_page_bid'),'monthly_searches':item.get('monthly_searches') or []})
    return rows
def parse_serp(task_results):
    paid=[]; organic=[]; types=Counter()
    for result in task_results:
        for block in result:
            for item in block.get('items') or []:
                typ=item.get('type') or 'unknown'; types[typ]+=1; dom=item.get('domain')
                if typ in {'paid','shopping'} and dom: paid.append(dom)
                if typ=='organic' and dom: organic.append(dom)
    return {'paid_domains':list(dict.fromkeys(paid)),'organic_domains':list(dict.fromkeys(organic)),'item_types':dict(types)}
def weighted_avg(rows,field):
    den=sum(num(r.get('search_volume')) for r in rows); return sum(num(r.get('search_volume'))*num(r.get(field)) for r in rows)/den if den else 0
def pct_rank_log(values,x):
    vals=sorted(math.log1p(max(0,v)) for v in values)
    if not vals:return 0
    lx=math.log1p(max(0,x)); return 100*sum(v<=lx for v in vals)/len(vals)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',required=True); ap.add_argument('--output-root',default='outputs'); args=ap.parse_args()
    login=os.getenv('DATAFORSEO_LOGIN'); pwd=os.getenv('DATAFORSEO_PASSWORD')
    if not login or not pwd: raise SystemExit('DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD required')
    cfg=json.load(open(args.config,encoding='utf-8')); stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'); out=Path(args.output_root)/f"{cfg['market_name'].lower().replace(' ','_')}_{stamp}"; out.mkdir(parents=True,exist_ok=True)
    api=DFS(login,pwd,out/'raw'); loc=cfg.get('location_code',2840); lang=cfg.get('language_code','en'); sug_limit=int(cfg.get('max_suggestions_per_seed',60)); serp_n=int(cfg.get('serp_sample_size',2))
    global_ex=re.compile(cfg.get('global_exclude_regex','a^'),re.I); high_re=re.compile(cfg.get('high_intent_regex','a^'),re.I); info_re=re.compile(r'\b(what is|how to|how do|guide|definition|meaning|diy|calculator|template|example|examples|history|wikipedia|jobs?|salary|career|training|course)\b',re.I)
    keyword_rows=[]; summaries=[]; serp_rows=[]; rejected_rows=[]
    for idx,n in enumerate(cfg['niches'],1):
        name=n['name']; seeds=[s.lower().strip() for s in n['seeds']]; inc=re.compile(n['include_regex'],re.I); exc=re.compile(n.get('exclude_regex','a^'),re.I); print(f'[{idx}/{len(cfg["niches"])}] {name}',flush=True)
        suggestions=[]
        for seed_idx,seed in enumerate(seeds,1):
            payload=[{'keyword':seed,'location_code':loc,'language_code':lang,'limit':sug_limit,'include_seed_keyword':True,'exact_match':True,'ignore_synonyms':True,'filters':['keyword_info.search_volume','>',0]}]
            suggestions.extend(extract_suggestions(api.post('dataforseo_labs/google/keyword_suggestions/live',payload,f'{idx:02d}_{name}_suggestions_{seed_idx:02d}')))
        pool={s for s in seeds}; pool.update(x['keyword'] for x in suggestions); qual=[]
        for kw in sorted(pool):
            ok=bool(inc.search(kw)) and not global_ex.search(kw) and not exc.search(kw)
            (qual if ok else rejected_rows).append(kw if ok else {'niche':name,'keyword':kw,'reason':'relevance_filter'})
        qual=sorted(set(qual)|set(seeds)); metrics=[]
        for b in range(0,len(qual),1000):
            payload=[{'keywords':qual[b:b+1000],'location_code':loc,'language_code':lang,'include_adult_keywords':False,'sort_by':'search_volume','tag':name}]
            metrics.extend(extract_volume(api.post('keywords_data/google_ads/search_volume/live',payload,f'{idx:02d}_{name}_volume_{b//1000+1}')))
            if b+1000<len(qual): time.sleep(5.2)
        bykw={r['keyword']:r for r in metrics}; rows=[]
        for kw in qual:
            r=bykw.get(kw,{'keyword':kw,'search_volume':0,'cpc':0,'competition':None,'competition_index':0,'low_top_of_page_bid':0,'high_top_of_page_bid':0,'monthly_searches':[]}); intent='low' if info_re.search(kw) else ('high' if high_re.search(kw) else 'medium')
            rr=dict(r); rr.update({'niche':name,'label':n.get('label',name),'category':n.get('category',''),'is_seed':kw in seeds,'intent':intent}); rows.append(rr); keyword_rows.append(rr)
        nonzero=[r for r in rows if num(r['search_volume'])>0]; hi=[r for r in nonzero if r['intent']=='high']; medhi=[r for r in nonzero if r['intent'] in {'high','medium'}]; qsv=sum(num(r['search_volume']) for r in nonzero); hsv=sum(num(r['search_volume']) for r in hi); mhsv=sum(num(r['search_volume']) for r in medhi); seed_found=sum(1 for s in seeds if num(bykw.get(s,{}).get('search_volume'))>0)
        target=sorted(hi or medhi or nonzero,key=lambda r:(num(r['search_volume']),num(r['cpc'])),reverse=True)[:serp_n]; paid=Counter(); organic=Counter()
        for j,r in enumerate(target,1):
            payload=[{'keyword':r['keyword'],'location_code':loc,'language_code':lang,'depth':10,'tag':name}]; parsed=parse_serp(api.post('serp/google/organic/live/regular',payload,f'{idx:02d}_{name}_serp_{j:02d}'))
            for d in parsed['paid_domains']: paid[d]+=1
            for d in parsed['organic_domains']: organic[d]+=1
            serp_rows.append({'niche':name,'keyword':r['keyword'],'search_volume':r['search_volume'],'cpc':r['cpc'],**parsed})
        avg_comp=statistics.mean([num(r['competition_index']) for r in medhi]) if medhi else 0
        summaries.append({'niche':name,'label':n.get('label',name),'category':n.get('category',''),'seed_count':len(seeds),'seed_nonzero_count':seed_found,'qualified_keyword_count':len(rows),'nonzero_keyword_count':len(nonzero),'qualified_monthly_search_volume':round(qsv),'high_intent_monthly_search_volume':round(hsv),'medium_plus_monthly_search_volume':round(mhsv),'high_intent_share':round(hsv/qsv,4) if qsv else 0,'weighted_cpc':round(weighted_avg(medhi,'cpc'),2),'high_intent_weighted_cpc':round(weighted_avg(hi,'cpc'),2),'max_high_top_of_page_bid':round(max([num(r['high_top_of_page_bid']) for r in medhi] or [0]),2),'avg_competition_index':round(avg_comp,1),'serp_keywords_sampled':len(target),'unique_paid_domains_seen':len(paid),'top_paid_domains':', '.join(d for d,_ in paid.most_common(8)),'top_organic_domains':', '.join(d for d,_ in organic.most_common(8))})
    svs=[r['high_intent_monthly_search_volume'] for r in summaries]; cpcs=[r['high_intent_weighted_cpc'] for r in summaries]; bids=[r['max_high_top_of_page_bid'] for r in summaries]
    for r in summaries:
        demand=pct_rank_log(svs,r['high_intent_monthly_search_volume']); cpc=pct_rank_log(cpcs,r['high_intent_weighted_cpc']); bid=pct_rank_log(bids,r['max_high_top_of_page_bid']); intent=min(100,r['high_intent_share']*125); comp=min(100,r['avg_competition_index']); ads=min(100,r['unique_paid_domains_seen']*25); r['search_opportunity_score']=round(.35*demand+.25*cpc+.15*bid+.10*intent+.10*comp+.05*ads,1)
        coverage=r['seed_nonzero_count']/r['seed_count'] if r['seed_count'] else 0; quality=100
        if coverage<.5: quality-=25
        if r['nonzero_keyword_count']<5: quality-=20
        if r['qualified_monthly_search_volume']==0: quality-=40
        if r['high_intent_monthly_search_volume']==0: quality-=20
        r['research_quality_score']=max(0,quality); r['confidence']='A' if quality>=90 else ('B' if quality>=75 else ('C' if quality>=55 else 'D'))
    summaries.sort(key=lambda r:r['search_opportunity_score'],reverse=True)
    kfields=['niche','label','category','keyword','is_seed','intent','search_volume','cpc','competition','competition_index','low_top_of_page_bid','high_top_of_page_bid','monthly_searches']; flat=[]
    for r in keyword_rows:
        x=dict(r); x['monthly_searches']=json.dumps(x.get('monthly_searches') or []); flat.append(x)
    write_csv(out/'keyword_metrics.csv',flat,kfields); write_csv(out/'niche_summary.csv',summaries,list(summaries[0].keys())); write_csv(out/'rejected_keywords.csv',rejected_rows,['niche','keyword','reason']); (out/'serp_samples.json').write_text(json.dumps(serp_rows,indent=2),encoding='utf-8')
    evals=[{'name':'all_niches_return_metrics','pass':all(r['qualified_keyword_count']>0 for r in summaries)},{'name':'90pct_niches_have_nonzero_demand','pass':sum(r['qualified_monthly_search_volume']>0 for r in summaries)>=math.ceil(len(summaries)*.9)},{'name':'90pct_seed_coverage','pass':sum(r['seed_nonzero_count'] for r in summaries)/max(1,sum(r['seed_count'] for r in summaries))>=.9},{'name':'no_filtered_keyword_violations','pass':all((re.search(next(n['include_regex'] for n in cfg['niches'] if n['name']==r['niche']),r['keyword'],re.I) or r['is_seed']) for r in keyword_rows)},{'name':'no_single_niche_absurd_10m_sv','pass':max(r['qualified_monthly_search_volume'] for r in summaries)<10_000_000}]
    eval_doc={'generated_at':datetime.now(timezone.utc).isoformat(),'passes':sum(e['pass'] for e in evals),'total':len(evals),'all_pass':all(e['pass'] for e in evals),'evals':evals,'api_reported_cost_estimate':round(api.total_cost,4)}; (out/'eval.json').write_text(json.dumps(eval_doc,indent=2),encoding='utf-8')
    lines=[f"# {cfg['market_name']} — Lead-Gen Search Underwriting",'',f"Generated: {datetime.now(timezone.utc).isoformat()}",'',f"Automated research evals: **{eval_doc['passes']}/{eval_doc['total']} passed**.",'',"This ranks **search-side lead-generation opportunity**, not final market attractiveness. Buyer capacity and accepted CPL must still be validated.",'','| Rank | Niche | Score | Qualified SV | High-intent SV | High-intent CPC | Max bid | Ads | Confidence |','|---:|---|---:|---:|---:|---:|---:|---:|---|']
    for i,r in enumerate(summaries,1): lines.append(f"| {i} | {r['label']} | {r['search_opportunity_score']:.1f} | {r['qualified_monthly_search_volume']:,} | {r['high_intent_monthly_search_volume']:,} | ${r['high_intent_weighted_cpc']:.2f} | ${r['max_high_top_of_page_bid']:.2f} | {r['unique_paid_domains_seen']} | {r['confidence']} |")
    lines += ['', '## Top keywords by niche','']
    for r in summaries[:12]:
        lines.append(f"### {r['label']}"); top=sorted([x for x in keyword_rows if x['niche']==r['niche'] and x['intent']!='low'],key=lambda x:num(x['search_volume']),reverse=True)[:8]; lines.append(', '.join(f"{x['keyword']} ({int(num(x['search_volume'])):,}/mo, ${num(x['cpc']):.2f} CPC)" for x in top) or 'No nonzero commercial keywords found.'); lines.append('')
    lines += ['## Interpretation','','- Qualified SV is intentionally conservative: only terms passing niche-specific agriculture relevance rules are counted.','- High-intent SV emphasizes quote, financing, dealer, contractor, insurance, service, purchase and similar commercial terms.','- Search Opportunity Score is relative within this farm-market universe and should not substitute for buyer validation.','- A market becomes a true GO only after we identify credible $10K–$100K/month-capable buyers and validate price/cap/acceptance rules.']; (out/'report.md').write_text('\n'.join(lines),encoding='utf-8'); print(f'Results written to {out}; evals {eval_doc["passes"]}/{eval_doc["total"]}; API cost field sum ${api.total_cost:.4f}',flush=True)
if __name__=='__main__': main()
