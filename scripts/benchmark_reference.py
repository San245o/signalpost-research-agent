"""Run the unmodified reference CLI with explicit, recorded common limits."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--repo',required=True,type=Path);p.add_argument('--manifest',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path);p.add_argument('--universe',type=Path)
    p.add_argument('--seconds',type=int,default=600);p.add_argument('--requests',type=int,default=2000)
    args=p.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    for name in ('SERPER_API_KEY','SERPER_API_KEYS','EXA_API_KEY','PARALLEL_API_KEY','TAVILY_API_KEY'):
        os.environ.pop(name,None)
    items=[json.loads(line) for line in args.manifest.read_text(encoding='utf-8').splitlines() if line.strip()]
    orgs=[str(row['organisation_number']) for row in items]
    plain=args.out/'input.txt';plain.write_text('\n'.join(orgs)+'\n',encoding='utf-8')
    sys.path.insert(0,str(args.repo.resolve()))
    import src.run_batch as reference
    Original=reference.BudgetLimits
    # The reference CLI has no time/request flags. Override only its budget
    # constructor; all discovery, validation, crawling and export code is native.
    reference.BudgetLimits=lambda **kw:Original(max_requests=args.requests,max_wall_clock_seconds=args.seconds,**kw)
    setup={'companies':len(orgs),'company_order_sha256':hashlib.sha256('\n'.join(orgs).encode()).hexdigest(),
           'seconds':args.seconds,'request_limit':args.requests,'concurrency':8,'paid_keys':False,'browser':False,
           'budget_adapter':'Only CLI BudgetLimits constructor overridden; reference source files unchanged',
           'manifest_format':'Same organisation numbers converted from JSONL to native plain-number input'}
    (args.out/'benchmark-setup.json').write_text(json.dumps(setup,indent=2),encoding='utf-8')
    sys.argv=['src.run_batch','--input',str(plain.resolve()),'--out',str(args.out.resolve()),'--concurrency','8']
    if args.universe:sys.argv+=['--universe',str(args.universe.resolve())]
    reference.main()


if __name__=='__main__':main()
