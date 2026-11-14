# Sources, cache provenance and licences

Official identity, roles, workplaces and filings come from Brønnøysundregistrene public APIs and optional supplied registry snapshots. Attribute Brønnøysundregistrene; public registry data is declared under NLOD 2.0: https://data.norge.no/nlod/en/2.0 . Separate-entity accounts and reporting years remain explicit.

NAV leads use the public token and vacancy feed. The original cached snapshot is integrity-checked and contributes ACTIVE UUID/name candidates only. Employer identity and current ad status/dates are fetched live. Snapshot completeness never substitutes for live verification.

## Original public-cache export

The bundled map currently contains 392,307 company keys and 531,108 website URL leads across the source register, with 21,463 social URL hints and 2,032 companies with NAV UUID leads. These include generated rare non-leading name-token .no guesses. They are candidate counts, not observed domain counts, eligible-universe coverage, verified websites or recall. The manifest distinguishes the extraction methods; every accepted website still requires fresh ownership proof.

The manifest records the compressed/uncompressed hashes and each source file hash. Row source indices refer to that manifest. Same-host owner-page hints are fetched afresh. Source acquisition times are unknown where the original files did not retain them; file modification times are explicitly not retrieval times.

Original allowlisted input files:

- official_411k_domains.json
- universe-websites.json
- wikidata-websites.json
- fagfolkguiden_harvested.json
- universe_linkedin_profiles.json
- universe_social_profiles.json
- wikidata_enriched.json
- brreg_companies.sqlite
- brreg_subunits.sqlite
- verified-sites.sqlite
- unique_crawler_domains.json
- nav-feed-snapshot.json

The public registry CSV also produces a separately hash-checked screening archive. It contains aggregate shared phone/email/website-holder lower bounds, not company profiles or current contact claims. The loader tolerates missing/corrupt optional screening without disabling valid website leads.

Wikidata structured data is CC0: https://www.wikidata.org/wiki/Wikidata:Licensing . Cached QIDs/social links require a fresh entity response with the exact Norwegian organisation number P2333. Deprecated, scoped and ended statements are withheld. Website claims bridged through that graph cite fresh graph and website evidence. Other original public maps retain their known file-level provenance; no stronger acquisition/licence guarantee is inferred from a filename. Only candidate URLs are exported, without cached page text, ratings or profile answers.

## Live websites, search and trust data

Company pages, feeds, sitemaps and directly linked ATS pages are fetched with public-address validation, DNS pinning, TLS/hostname checks, robots and request limits. Restricted platform destinations are not scraped; links establish only supported profile URLs. DuckDuckGo Instant Answer is a candidate-only free fallback. Paid adapters require explicit mode selection and environment keys; no key is embedded or read from an .env file.

The Mozilla CA bundle is redistributed unchanged from certifi 2026.10.23. Its SHA-256 and source URL are in data/trust/manifest.json. The upstream MPL-2.0 notice is retained in data/trust/LICENSE.txt. Source certificate data and licence: https://github.com/certifi/python-certifi and https://mozilla.org/MPL/2.0/ . It supplements system trust without disabling certificate or hostname verification.

The release contains no imported participant candidate tables, fitted model or runtime code module. Local historical research and comparison material are excluded from packaging.

During the version 2.7 live check, the DuckDuckGo API host returned `Disallow: /` in robots.txt, so queries were withheld. The adapter supports the infobox Website field but cannot guarantee usable live access.

## Independently observed website candidates

`data/observed-website-seeds.jsonl.gz` is produced by `scripts/build_observed_seeds.py` from an explicitly supplied public host list and the official public registry universe. The initial bounded build selected 160 hosts from the original `unique_crawler_domains.json`, completed all 160, used 479 requests, and retained 10 companies with exact owned identifiers. Input and output hashes and the acquisition policy are in its manifest.

The output contains URL/organisation-number pairs, owner-page pointers, actual retrieval times and body hashes. It contains no copied page text or precomputed profiles. Raw source captures and resumable crawl state stay outside the release under `runs/`. The public registry source remains attributed to Brønnøysundregistrene under NLOD 2.0. Live website content remains with its publishers; no content licence is inferred. Candidate pairs never substitute for fresh ownership evidence. No participant URL table, host list or source-code module is an input to this build.

When the default candidate map is enabled, this independently acquired index precedes speculative URL guesses. A missing or corrupt observed index leaves valid original candidates usable. `--no-candidate-map` disables both maps. A custom candidate map is used on its own.

## Linked public job boards

The verified website must link to the provider board before its public job listing endpoint is used. Provider and tenant paths are matched against that chain; each published advertisement cites its raw JSON item and the captured linking pages. No API credentials or third-party source modules are bundled. Greenhouse's [public Job Board API](https://docs.greenhouse.io/job-board.html) and Lever's [public Postings API](https://github.com/lever/postings-api) document these endpoints. Advertisements without supported employer/date/status evidence remain explicitly labelled as observations. Board modification times are not publication dates. Publisher rights remain with the original sources.
