"""Build the release cache exclusively from the original public source files."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.build_public_seeds import build as build_original


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--original-cache',required=True,type=Path)
    p.add_argument('--out',type=Path,default=ROOT/'data')
    a=p.parse_args()
    original=build_original(a.original_cache,a.out/'public-website-seeds.jsonl.gz')
    print(json.dumps({'original':{k:original[k] for k in ('companies','website_urls','social_url_hints','archive_sha256')},
                      'sources':len(original['sources']),
                      'runtime_downloads_required':False,'notice':'Source declarations: docs/SOURCES.md.'},indent=2))
