"""Replay captured HTTP responses offline and check semantic refresh idempotence."""
import argparse
import json
import hashlib
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from signalpost.inputs import load_manifest
from signalpost.net import Response
from signalpost.records import atomic_write,canonical,semantic_claims
from signalpost.runner import run_batch
from signalpost.viewer import render


def replay(source,output):
    source,output=Path(source),Path(output)
    metadata={}
    for p in (source/'sources').glob('*.json'):
        row=json.loads(p.read_text(encoding='utf-8'))
        metadata[(row['final_url'],row['content_sha256'])]=row
    lookup={}
    for line in (source/'attempts.jsonl').read_text(encoding='utf-8').splitlines():
        row=json.loads(line);key=(row.get('url'),row.get('content_sha256'));meta=metadata.get(key,{})
        digest=row.get('content_sha256');path=source/'sources'/((digest or '')+'.bin')
        body=path.read_bytes() if path.exists() else b''
        if body and hashlib.sha256(body).hexdigest()!=digest:
            raise ValueError('Saved source hash mismatch: '+str(digest))
        lookup[row['url']]=Response(row['url'],row.get('status',0),body,meta.get('response_headers',{}),
                                    row.get('retrieved_at',''),row.get('reason',''),row.get('state','failed'),
                                    requested_url=row.get('requested_url') or row['url'],redirect_chain=row.get('redirect_chain',[]))
    def transport(url,headers):
        import copy
        return copy.copy(lookup[url]) if url in lookup else Response(url,error='URL was not in the saved run')
    old=[json.loads(line) for line in (source/'submission.jsonl').read_text(encoding='utf-8').splitlines()]
    # Seed the history with the original, with original capture locations recorded.
    from signalpost.records import refresh
    for row in old:refresh(row,output/'snapshots',captures=source/'sources')
    rows,report=run_batch(load_manifest(source/'manifest.jsonl'),output,transport=transport,
                         search_mode='none',max_seconds=120,request_budget=len(old)*50)
    same=all(semantic_claims(a)==semantic_claims(b) for a,b in zip(old,rows))
    no_false_changes=all(not row['changes'] for row in rows)
    verdict={'passed':same and no_false_changes,'companies':len(rows),'semantic_facts_unchanged':same,
             'no_false_changes':no_false_changes,'network_access':False,
             'new_material_versions':sum(not row['refresh']['unchanged'] for row in rows)}
    atomic_write(output/'index.html',render(rows,report))
    atomic_write(output/'replay-report.json',json.dumps(verdict,indent=2));return verdict


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();report=replay(args.run,args.out);print(json.dumps(report,indent=2));raise SystemExit(0 if report['passed'] else 1)
