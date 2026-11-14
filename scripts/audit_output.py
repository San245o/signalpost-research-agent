"""Audit a run's saved contract, source hashes, exact spans and synthesis references."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import re
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from signalpost.net import Response
from signalpost.records import validate, atomic_write


def audit(directory, expected=None):
    directory=Path(directory)
    rows=[json.loads(line) for line in (directory/'submission.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    failures=[]; cache={}; claims=0; evidence=0; sentence_count=0; errors=0
    for i,env in enumerate(rows):
        try:
            validate(env)
            if any(key in env for key in ('website_discovery','crawl_attempts','external_discovery')):
                raise ValueError('Unpublished candidate diagnostics leaked into submission')
            unpublished=[c.get('reason','') for c in env['claims'] if c['availability']!='available']+[e.get('message','') for e in env['errors']]
            if any(re.search(r'https?://',str(value)) for value in unpublished):
                raise ValueError('Unpublished explanation contains a URL')
            ids={e['id'] for e in env['evidence']}
            for e in env['evidence']:
                digest=e['content_sha256']
                if digest not in cache:
                    body=(directory/'sources'/(digest+'.bin')).read_bytes()
                    if hashlib.sha256(body).hexdigest()!=digest:raise ValueError('Source hash mismatch')
                    cache[digest]=body
                # Captured JSON is UTF-8. HTML may declare a different charset;
                # test its actual saved byte decodings without normalizing quotes.
                body=cache[digest]
                encodings=('utf-8','iso-8859-1','windows-1252')
                if not any(e['claim_span'] in body.decode(enc,errors='replace') for enc in encodings):
                    raise ValueError('Claim span absent from source capture')
                evidence+=1
            for sentence in env.get('summary',{}).get('sentences',[]):
                if not sentence.get('evidence_ids') or not set(sentence['evidence_ids']).issubset(ids):
                    raise ValueError('Synthesis has missing/dangling evidence')
                sentence_count+=1
            claims+=sum(c['availability']=='available' for c in env['claims'])
            errors+=len(env['errors'])
        except (ValueError,KeyError,OSError,TypeError) as exc:
            failures.append({'index':i,'organisation_number':env.get('organisation_number'),'reason':str(exc)})
    if expected is not None and len(rows)!=expected:failures.append({'reason':'Unexpected output count'})
    all_captures=list((directory/'sources').glob('*.bin'))
    for path in all_captures:
        if path.stem in cache:continue
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest()!=path.stem:
                failures.append({'capture':path.name,'reason':'Uncited source hash mismatch'})
        except OSError as exc:failures.append({'capture':path.name,'reason':str(exc)})
    aliases_equal=(directory/'submission.jsonl').read_bytes()==(directory/'envelopes.jsonl').read_bytes()
    if not aliases_equal:failures.append({'reason':'Output aliases differ'})
    report={'passed':not failures,'envelopes':len(rows),'unique_companies':len({r['organisation_number'] for r in rows}),
            'terminal_states':dict(Counter(r['run']['terminal_status'] for r in rows)),
            'available_claims':claims,'evidence_records':evidence,'unique_source_captures':len(cache),
            'cited_summary_sentences':sentence_count,'company_error_records':errors,
            'all_source_captures_checked':len(all_captures),
            'rejected_capture_files':len(list((directory/'sources').glob('*.rejected'))),
            'output_aliases_identical':aliases_equal,'failures':failures,
            'scope':'Structural integrity, captured-byte hashes and span/reference validity. Not a factual accuracy or official-score audit.'}
    atomic_write(directory/'audit-report.json',json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory');p.add_argument('--expected',type=int)
    args=p.parse_args();report=audit(args.directory,args.expected);print(json.dumps(report,indent=2));raise SystemExit(0 if report['passed'] else 1)
