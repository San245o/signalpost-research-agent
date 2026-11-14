from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from .inputs import load_manifest, load_snapshot
from .net import utcnow
from .records import atomic_write
from .runner import run_batch
from .viewer import render
from .seeds import DEFAULT as DEFAULT_SEEDS


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    parser = argparse.ArgumentParser(description="Source-backed Norwegian company research. Python 3.11+, no install, no keys.")
    parser.add_argument("batch", nargs="?", help="Evaluator manifest (or use --manifest)")
    parser.add_argument("--manifest", "--input", "--organisations", "-i", dest="manifest")
    parser.add_argument("--out", "--output", "-o", required=True, help="Output directory or .jsonl file")
    parser.add_argument("--bulk", "--universe", "--snapshot", dest="bulk", type=Path, help="Optional frozen CSV/JSONL snapshot, optionally gzipped")
    parser.add_argument("--workers", "--concurrency", type=int, default=8)
    parser.add_argument("--max-seconds", type=float, default=900, help="Whole-batch research deadline (default 900s)")
    parser.add_argument("--company-seconds", type=float, default=60)
    parser.add_argument("--timeout", type=float, default=5, help="Maximum individual HTTP socket timeout")
    parser.add_argument("--request-budget", type=int, help="Total outbound attempts, including robots/retries/redirects; default 20 per input")
    parser.add_argument("--candidate-cap", type=int, default=3)
    parser.add_argument("--page-cap", type=int, default=8)
    parser.add_argument("--snapshots", type=Path, help="Persistent append-only history directory; defaults to OUT/snapshots")
    parser.add_argument("--registry-only", action="store_true", help="Skip company-site acquisition; useful for execution smoke tests")
    parser.add_argument("--search", choices=["auto", "free", "none", "serper", "exa", "parallel"], default="free", help="Default free Instant Answer discovery; paid providers require explicit selection and environment keys")
    parser.add_argument("--paid-search-budget", type=int, default=None, help="Shared Serper credit cap; default half the batch size when a key exists")
    parser.add_argument("--paid-search-per-company", type=int, default=4)
    parser.add_argument("--nav-pages", type=int, default=80, help="Shared recent NAV feed page cap (0 disables)")
    parser.add_argument('--candidate-map',type=Path,default=DEFAULT_SEEDS,help='Bundled public URL leads; never accepted without fresh ownership proof')
    parser.add_argument('--no-candidate-map',action='store_true')
    parser.add_argument('--candidate-map-url',help='Optional declared HTTPS candidate map; requires --candidate-map-sha256')
    parser.add_argument('--candidate-map-sha256')
    parser.add_argument('--cache-dir',type=Path,help='Optional persistent successful HTTP/search responses and NAV state')
    parser.add_argument('--fresh',action='store_true',help='Bypass persisted HTTP/search response cache')
    parser.add_argument('--browser',action='store_true',help='Optional Playwright rendering for confirmed JS shells')
    parser.add_argument('--browser-max-renders',type=int,default=25)
    parser.add_argument('--spend-cap-usd',type=float,help='Optional shared paid-search dollar cap; requires declared provider rates')
    parser.add_argument('--no-public-search',action='store_true',help='Disable the single free Instant Answer discovery round')
    parser.add_argument('--paid-categories',default='websites,news,places',help='Optional paid discovery categories, comma-separated')
    parser.add_argument('--website-paid-rounds',type=int,default=1,help='Serper website calls per company; default 1 preserves credits for news/Places')
    args = parser.parse_args(argv)
    source = args.manifest or args.batch
    if not source:
        parser.error("Supply the evaluator's manifest using --manifest FILE")
    if not 1 <= args.workers <= 64 or min(args.max_seconds, args.company_seconds, args.timeout) <= 0:
        parser.error("Workers must be 1..64 and timeout/deadline values positive")
    if args.request_budget is not None and args.request_budget < 0 or min(args.candidate_cap, args.page_cap) < 0:
        parser.error("Request and page limits cannot be negative")
    if args.paid_search_budget is not None and args.paid_search_budget < 0 or min(args.paid_search_per_company, args.nav_pages) < 0:
        parser.error("Paid credit caps cannot be negative")
    if args.browser_max_renders<0 or args.spend_cap_usd is not None and args.spend_cap_usd<0:
        parser.error('Browser and dollar caps cannot be negative')
    if args.candidate_map_url and not args.candidate_map_sha256:
        parser.error('A hosted candidate map requires its declared SHA-256')
    categories=tuple(filter(None,args.paid_categories.split(',')))
    if set(categories)-{'websites','news','places'} or args.website_paid_rounds<0:
        parser.error('Invalid paid categories or website round cap')
    try:
        items = load_manifest(source)
    except (OSError, ValueError) as exc:
        print(f"Cannot read evaluator manifest: {exc}", file=sys.stderr)
        return 2
    requested = Path(args.out)
    directory = requested.parent if requested.suffix.lower() == ".jsonl" else requested
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Snapshot loading is optional and bounded by local file I/O, no download.
        seeds, warnings = load_snapshot(args.bulk, {item['organisation_number'] for item in items})
        for warning in warnings:
            print(warning, file=sys.stderr)
        envelopes, report = run_batch(items, directory, workers=args.workers, max_seconds=args.max_seconds,
                    company_seconds=args.company_seconds, timeout=args.timeout,
                    request_budget=args.request_budget, snapshots=args.snapshots, seeds=seeds, warnings=warnings,
                    websites=not args.registry_only, candidate_cap=args.candidate_cap, page_cap=args.page_cap,
                    search_mode=args.search, paid_credits=args.paid_search_budget, paid_per_company=args.paid_search_per_company, nav_pages=args.nav_pages,
                    candidate_map=None if args.no_candidate_map else args.candidate_map,candidate_map_url=args.candidate_map_url,
                    candidate_map_sha256=args.candidate_map_sha256,cache_dir=args.cache_dir,fresh=args.fresh,
                    browser=args.browser,browser_max_renders=args.browser_max_renders,spend_cap=args.spend_cap_usd,
                    public_search=not args.no_public_search,paid_categories=categories,website_paid_rounds=args.website_paid_rounds)
        if requested.suffix.lower() == ".jsonl" and requested != directory / "submission.jsonl":
            atomic_write(requested, (directory / "submission.jsonl").read_text(encoding="utf-8"))
        try:
            atomic_write(directory / "index.html", render(envelopes, report))
        except Exception as exc:
            print(f"Results are saved; viewer generation failed: {type(exc).__name__}", file=sys.stderr)
        print(json.dumps({"input_count": len(items), "envelope_count": len(envelopes), "requests_used": report["requests_used"],
                          "terminal_states": report["terminal_states"], "output": str(requested.resolve())}, ensure_ascii=False))
        return 0
    except OSError as exc:
        print(f"Cannot persist evaluator output: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
