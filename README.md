# Signalpost

Research Norwegian companies from a supplied organisation-number batch. Python 3.11+ and its standard library are sufficient. The default run needs no API key, package installation, private Desktop file or mandatory download.

The same agent and run command support Windows, Linux and macOS. CI checks CLI startup, the offline pipeline and source packaging on all three operating systems with Python 3.11, 3.12 and 3.13, with site packages disabled. On systems where the interpreter is named `python3`, use `python3` in place of `python` in the commands below.

## Run

    python -S run.py --manifest OFFICIAL_BATCH.jsonl --out out --search free

Use the organiser's limits when provided:

    python -S run.py --manifest OFFICIAL_BATCH.jsonl --out out --search free --max-seconds 600 --request-budget 2000

Optional frozen public registry snapshot:

    python -S run.py --manifest BATCH.jsonl --bulk SNAPSHOT.csv.gz --out out --search free

Inputs support JSONL, JSON arrays, CSV, plain organisation numbers and gzip. Input order and duplicate inputs are preserved. A missing optional snapshot falls back to live registry requests.

## Research

Official registry identity, company-only annual accounts, roles and paginated workplaces anchor the entity. Website leads come from registered links, NAV, the bundled export of original public caches, business-email domains and name variants. Rare substantial name tokens supply additional candidates, including operating-brand domains absent from the observed URL list. Every accepted website needs freshly fetched ownership evidence; shared domains, foreign organisation numbers, holding-company conflicts and unresolved namesakes cause abstention.

Free search uses a bounded DuckDuckGo Instant Answer lookup. Its infobox website field and other URLs are candidates, never claim evidence. `--search none` disables search. Paid adapters require an explicit `--search auto|serper|exa|parallel` selection and environment credentials. The default `free` mode never uses paid keys, even if they are present in the environment.

Verified sites lead to contact/about pages, sitemaps, dated company publications, feeds, WordPress and careers/ATS pages. The parser understands nested JSON-LD graphs and references, scoped microdata (including itemref), RDFa and common business subtypes. A readable main-content fallback supplies a company description when metadata is absent; navigation, cookie notices and customer testimonials are excluded.

HTML role cards and explicit inline recruitment ads are labelled as observed advertisements. Inline ads require a specific role, qualification/responsibility text and an application instruction. Linked public Teamtailor, Greenhouse and Lever boards retain individual raw item spans and the website-to-board link chain. Wrong tenants, prospect pools, expired advertisements and generic careers headings are withheld. Posting dates, expiry dates and current status are never inferred from a board's modification timestamp. Structured job ads and NAV details must satisfy employer and current-date/status checks. Recruiter advertisements do not establish employment at the agency. Dated advice and product articles remain distinct from company announcements.

TLS uses system trust plus the bundled, hash-checked Mozilla CA data. Certificate and hostname verification remain enforced. DNS addresses are public-validated and pinned; redirects repeat policy checks. Transient network failures receive bounded retries. Failed robots retrieval never grants crawl permission. An unreachable HTTPS candidate can try its bare/`www` counterpart; each hostname gets independent DNS, TLS and robots checks. Explicit robots denials stop recovery, and no HTTP downgrade is introduced.

## Cache and refresh

The original public-cache export, public registry screening aggregates and our own observed-domain index are active. Build them from original source files:

    python -S scripts/build_cache.py --original-cache PATH/TO/local-cache

The build reads an explicit allowlist, opens SQLite read-only and exports URL/UUID candidates, source hashes and screening counts. It does not export reference profiles, evaluation answers, ratings or credentials. It also reads the original public registry CSV and NAV snapshot beside `local-cache` when present. Cached facts are never passed off as fresh evidence. See [source declarations](docs/SOURCES.md).

Build or resume the independent reverse index from an explicit public host list:

    python -S scripts/build_observed_seeds.py --domains PUBLIC_DOMAINS.json --universe PUBLIC_UNIVERSE.jsonl.gz --state runs/website-index --out data/observed-website-seeds.jsonl.gz --limit 500 --requests 1500 --seconds 300

The builder checks permitted live pages for ownership-labelled organisation numbers. It exports candidate URLs, owner-page pointers, acquisition times and hashes. Raw page captures stay in local state. Its bundled results are re-fetched and verified at runtime. The observed index is optional and hash-checked.

`--no-candidate-map` disables the bundled leads and screening. Missing/corrupt optional data degrades to live discovery. `--cache-dir cache` optionally reuses successful HTTP responses with original acquisition times; `--fresh` bypasses that response cache. `--snapshots history` shares refresh history across runs. Failed refreshes retain supported prior facts with visible stale flags.

## Output and verification

`submission.jsonl` and `envelopes.jsonl` contain identical flat terminal envelopes, exactly one per input. Each has `run`, `claims`, `evidence`, `summary`, `changes`, `errors` and `operations`. Available claims cite captured bytes, retrieval times and verbatim spans. Candidate/rejection ledgers are separate files. `index.html` is the self-contained research viewer. Output replacement retries temporary Windows permission errors while preserving the previous complete file; persistent filesystem denial is still reported.

The viewer provides six research topics, cited business-question briefs, financial cards, reporting-period selection, registered leadership/locations, a dated publication timeline and explicit hiring relationships. Search, evidence filters, revenue/employee sorting, two-to-four-company comparison, evidence dialogs and JSONL export work offline. Derived ratios require matching periods and currencies; every displayed metric cites its underlying filings. Unknown and stale observations stay visible. The layout supports keyboard navigation, mobile screens and dark mode.

    python -S -m unittest discover -s tests -t .
    python -S scripts/audit_output.py out --expected 100
    python -S scripts/package_source.py

Optional browser rendering uses the separately pinned `requirements-browser.txt` and `--browser`. Default research stays static and keyless. See [verification](docs/VERIFICATION.md) and [limitations](docs/LIMITATIONS.md). Local tests and publication counts do not establish official accuracy, recall, qualification or ranking.

## Submission and showcase

Agent: **Signalpost Research Agent 3.1**. Default research uses no LLM, paid API or third-party Python package. Expected external API/model fees are **US$0 per official run** in the default keyless mode; evaluator compute and network costs are excluded. Optional paid adapters stay disabled unless explicitly selected.

[100-company smoke and validation report](docs/VERIFICATION.md) · [Machine-readable verification](docs/verification.json) · [Models, APIs, cache provenance and licences](docs/SOURCES.md)

[JBOX product showcase](https://signalpost-builderr-sankeerth-preview.sites.jboxai.com/) is the existing separately deployed showcase. Its deployment and historical sample data are separate from this agent release. The current agent also writes its own offline research viewer to `out/index.html`.

The current 100-company validation replays previously acquired public responses and is labelled offline. The report separately identifies the previous full live cohort and the focused live check. No precomputed profiles are inputs to official research.
