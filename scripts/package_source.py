"""Build a clean source archive, excluding credentials, captures and local state."""
from __future__ import annotations
import hashlib
import json
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def build():
    dist=ROOT/'dist';dist.mkdir(exist_ok=True)
    files=[ROOT/'run.py',ROOT/'.gitignore',ROOT/'.gitattributes',ROOT/'.env.example',*ROOT.glob('*.md'),*ROOT.glob('requirements*.txt')]
    files.extend(p for p in (ROOT/'.github').rglob('*.yml') if p.is_file())
    for directory in ('signalpost','tests','scripts','docs','data'):
        files.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.md','.json','.jsonl'})
    files=sorted(set(files))
    seeds=ROOT/'data'/'public-website-seeds.jsonl.gz'
    if not seeds.exists():raise ValueError('Required public candidate map missing')
    files.append(seeds)
    manifest=json.loads(seeds.with_suffix('.manifest.json').read_text(encoding='utf-8'))
    if hashlib.sha256(seeds.read_bytes()).hexdigest()!=manifest['archive_sha256']:raise ValueError('Original cache hash mismatch')
    if manifest.get('screening'):
        entry=manifest['screening'];path=seeds.parent/entry['filename']
        if path.parent!=seeds.parent or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:raise ValueError('Screening cache hash mismatch')
        files.append(path)
    observed=ROOT/'data/observed-website-seeds.jsonl.gz'
    if observed.exists():
        declared=json.loads(observed.with_suffix('.manifest.json').read_text(encoding='utf-8'))
        if hashlib.sha256(observed.read_bytes()).hexdigest()!=declared['archive_sha256']:raise ValueError('Observed candidate map hash mismatch')
        files.append(observed)
    trust=ROOT/'data/trust'
    trust_manifest=json.loads((trust/'manifest.json').read_text(encoding='utf-8'))
    if hashlib.sha256((trust/'cacert.pem').read_bytes()).hexdigest()!=trust_manifest['sha256']:raise ValueError('CA bundle hash mismatch')
    required=[ROOT/'docs/SOURCES.md',trust/'cacert.pem',trust/'LICENSE.txt']
    for path in required:
        if not path.is_file():raise ValueError('Required data notice missing: '+str(path))
    files.extend(required)
    files=sorted(set(files))
    runtime=[p for p in files if p==ROOT/'run.py' or p.parent==ROOT/'signalpost']
    digests={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime}
    report={'archive':'signalpost-retry-source.zip','runtime_files':digests,
            'runtime_sha256':hashlib.sha256(json.dumps(digests,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'files':len(files),'contains_credentials':False,'contains_local_caches':False,
            'contains_public_candidate_map':True,'contains_imported_participant_data':False,
            'contains_required_data_notices':True}
    with zipfile.ZipFile(dist/report['archive'],'w',zipfile.ZIP_DEFLATED) as archive:
        for file in files:archive.write(file,str(file.relative_to(ROOT)).replace('\\','/'))
    report['archive_sha256']=hashlib.sha256((dist/report['archive']).read_bytes()).hexdigest()
    (dist/'source-manifest.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    return report


if __name__=='__main__':build()
