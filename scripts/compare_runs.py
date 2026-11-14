"""Compare actual publications on the same inputs; never infer an official score."""
import argparse
import collections
import json
from pathlib import Path


def read(directory):
    directory=Path(directory)
    rows=[json.loads(l) for l in (directory/'envelopes.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    report=json.loads((directory/'run-report.json').read_text(encoding='utf-8'))
    coverage=collections.Counter();claims=collections.Counter();sites={}
    for row in rows:
        available=[c for c in row['claims'] if c['availability']=='available' and not c.get('stale')]
        coverage.update({c['field'] for c in available});claims.update(c['field'] for c in available)
        site=next((c for c in available if c['field']=='official_website'),None)
        if site:sites[row['organisation_number']]=site['value']
    return rows,{'companies':len(rows),'coverage':dict(coverage),'claim_counts':dict(claims),'sites':sites,
                 'requests':report.get('requests_used'),'seconds':report.get('wall_clock_seconds'),
                 'company_errors':sum(len(r.get('errors',[])) for r in rows)}


def compare(reference,retry,output):
    his,h=read(reference);ours,o=read(retry)
    same=[r['organisation_number'] for r in his]==[r['organisation_number'] for r in ours]
    report={'same_input_order':same,'reference':h,'retry':o,
            'reference_only_websites':{k:v for k,v in h['sites'].items() if k not in o['sites']},
            'retry_only_websites':{k:v for k,v in o['sites'].items() if k not in h['sites']},
            'scope':'Published-output counts on one local cohort. No independent accuracy, recall, qualification or official score is inferred.'}
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'same_input_order':same,'reference_sites':len(h['sites']),'retry_sites':len(o['sites']),
                      'reference_social_companies':h['coverage'].get('company_profile',0),'retry_social_companies':o['coverage'].get('company_profile',0)},indent=2))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--reference',required=True);p.add_argument('--retry',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();compare(args.reference,args.retry,args.out)
