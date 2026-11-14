"""Daemon workers; only the coordinator publishes. A deadline always produces output."""
from __future__ import annotations
import json
import queue
import threading
import time
import hashlib
import sys
from collections import Counter
from pathlib import Path
from .net import Budget, Client, utcnow
from .records import Profile, atomic_write, canonical, refresh, validate, public_envelope
from .registry import identity, accounts, roles, workplaces, registry_events
from .web import collect_site
from .search import Serper
from .nav import NavIndex
from .cache import ResponseCache
from .seeds import DEFAULT as DEFAULT_SEEDS, load_seeds
from .strategies import STRATEGIES, VERSION


def research(item, run_id, sources, client, seed=None, *, websites=True, candidate_cap=3, page_cap=5):
    started = time.monotonic()
    profile = Profile(item["organisation_number"], run_id, sources)
    if not item.get("input_valid", True):
        profile.error("input", "Expected a nine-digit organisation number")
        return profile.finish(status="failed"), None
    record = None
    try:
        record = identity(profile, client, seed)
    except Exception as exc:
        profile.error("identity", f"{type(exc).__name__}: {exc}")
    if record:
        for stage in (accounts, roles, workplaces, registry_events):
            try:
                stage(profile, client)
            except Exception as exc:
                profile.error(stage.__name__, f"{type(exc).__name__}: {exc}")
        if websites:
            try:
                collect_site(profile, client, record, candidate_cap=candidate_cap, page_cap=page_cap)
            except Exception as exc:
                profile.error("website", f"{type(exc).__name__}: {exc}")
        else:
            profile.missing("official_website", "not_available", "Website acquisition disabled by run configuration")
    # An unresolved identity is an explicit failed result, never a missing row.
    return profile.finish(requests=client.budget.by_company[profile.org], runtime_ms=(time.monotonic() - started) * 1000,
                          status="completed" if record else "failed"), record


def failed_result(item, run_id, sources, reason, budget=None):
    profile = Profile(item["organisation_number"], run_id, sources)
    profile.error("batch", reason)
    return profile.finish(status="failed", requests=budget.by_company[profile.org] if budget else 0)


def percentile(values, q):
    if not values:
        return 0
    rows = sorted(values)
    return rows[min(len(rows) - 1, int((len(rows) - 1) * q))]


def finalize_captures(results, sources, items, run_id, budget):
    """Reconcile the store after workers finish; never publish a corrupt source."""
    bad=[];checked=0;failed_companies=0;quarantined=[]
    for path in sources.glob('*.bin'):
        checked+=1
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest()==path.stem:continue
            reason='Saved bytes do not match the content-addressed filename'
        except OSError as exc:reason='Source readback failed: '+type(exc).__name__
        bad.append((path,reason))
    invalid={p.stem for p,_ in bad}
    for i,env in list(results.items()):
        if any(e.get('content_sha256') in invalid for e in env.get('evidence',[])):
            results[i]=failed_result(items[i],run_id,sources,'Captured evidence failed final integrity verification',budget)
            failed_companies+=1
    for path,reason in bad:
        # Retain the original file for diagnosis; it is not valid claim evidence.
        try:
            target=path.with_suffix('.rejected');path.replace(target)
            quarantined.append({'digest':path.stem,'file':target.name,'reason':reason})
        except OSError:
            quarantined.append({'digest':path.stem,'file':path.name,'reason':reason,'quarantine_failed':True})
    return {'checked':checked,'rejected':quarantined,'failed_cited_companies':failed_companies}


def run_batch(items, output, *, run_id=None, snapshots=None, workers=8, max_seconds=900,
              request_budget=None, timeout=5, company_seconds=60, seeds=None, warnings=None,
              websites=True, candidate_cap=3, page_cap=5, transport=None, processor=None,
              search_mode="free", paid_credits=None, paid_per_company=4, nav_pages=80,
              candidate_map=DEFAULT_SEEDS, candidate_map_url=None, candidate_map_sha256=None,
              cache_dir=None, fresh=False, browser=False, browser_max_renders=25,
              spend_cap=None, public_search=True,paid_categories=('websites','news','places'),website_paid_rounds=1):
    start = time.monotonic()
    run_id = run_id or utcnow().replace(":", "-")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    sources = output / "sources"
    sources.mkdir(exist_ok=True)
    snapshot_dir = Path(snapshots) if snapshots else output / "snapshots"
    budget = Budget(request_budget if request_budget is not None else len(items) * 20, max_seconds)
    disk_cache=ResponseCache(cache_dir) if cache_dir else None
    renderer=None
    if browser:
        from .browser import BrowserRenderer
        renderer=BrowserRenderer(max_renders=browser_max_renders)
    seed_client=Client(budget,'shared_candidates',timeout=timeout,company_seconds=min(30,max_seconds*.1),
                       transport=transport,captures=sources,disk_cache=disk_cache,fresh=fresh)
    website_seeds,seed_report=load_seeds({item['organisation_number'] for item in items},path=candidate_map,
                url=candidate_map_url,sha256=candidate_map_sha256,client=seed_client) if candidate_map or candidate_map_url else ({},{'disabled':True})
    processor = processor or research
    results = {}
    records = {}
    warnings = list(warnings or [])
    discovery_done = set()
    nav = NavIndex(state_path=Path(cache_dir)/'nav-index.json' if cache_dir else None)
    if websites and nav_pages:
        nav_client = Client(budget, "shared_nav", timeout=timeout, company_seconds=min(240, max_seconds * .5, max(10,len(items)*2.5)), transport=transport,captures=sources,disk_cache=disk_cache,fresh=fresh)
        threading.Thread(target=nav.build, args=(nav_client, nav_pages), name="nav-feed", daemon=True).start()
    journal = output / "envelopes.jsonl"
    attempts_path = output / "attempts.jsonl"
    journal.write_text("", encoding="utf-8")
    interrupted = False
    last_checkpoint=[time.monotonic()]
    def checkpoint():
        # Each checkpoint contains one row per completed input, never duplicate
        # core/discovery rows. Both conventional names stay readable during work.
        body = "".join(canonical(public_envelope(results[i])) + "\n" for i in sorted(results))
        atomic_write(journal, body)
        atomic_write(output / "submission.jsonl", body)
    with attempts_path.open("w", encoding="utf-8") as attempts:
        def ledger_write(org, ledger):
            for row in ledger:
                attempts.write(canonical({"organisation_number": org, **row}) + "\n")
            attempts.flush()
        def phase(indices, enrich=False, search=None, nav_only=False):
            todo, done = queue.Queue(), queue.Queue()
            stop = threading.Event()
            for i in indices:
                todo.put(i)
            # Core identity/registry receives at most 65% of the run's time when
            # discovery is enabled. All companies get their official phase first.
            phase_end = budget.deadline if enrich or not websites else start + max_seconds * .65
            def worker():
                while not stop.is_set():
                    try:
                        i = todo.get_nowait()
                    except queue.Empty:
                        return
                    item = items[i]
                    client = Client(budget, item["organisation_number"], timeout=timeout,
                                    company_seconds=company_seconds, transport=transport,captures=sources,
                                    disk_cache=disk_cache,fresh=fresh,browser=renderer)
                    client.deadline = min(client.deadline, phase_end)
                    try:
                        if enrich:
                            profile = Profile.resume(results[i], sources)
                            before = time.monotonic()
                            records[i]['_operating_names']=profile.data.get('operating_names',[])
                            records[i]['_public_hints'] = seed_report.get('company_hints',{}).get(item['organisation_number'],{})
                            records[i]['_shared_contacts'] = seed_report.get('screening',{})
                            records[i]['_nav_homepages'] = nav.add_jobs(profile, records[i], client)
                            records[i]['_website_seeds'] = website_seeds.get(item['organisation_number'],[])
                            records[i]['_website_proof_pages'] = seed_report.get('verification_pages',{}).get(item['organisation_number'],[])
                            records[i]['_subunit_website_leads']=[a['url'] for a in profile.data.get('website_discovery',[])
                                                                if a.get('origin')=='live_registry_subunit']
                            if not nav_only:
                                from .discovery import enrich_public_hints
                                try:
                                    live_leads=enrich_public_hints(profile,client,records[i])
                                    records[i]['_website_seeds']=list(dict.fromkeys(live_leads+records[i]['_website_seeds']))[:6]
                                except Exception as exc:profile.error('public_discovery','Public candidate verification failed: '+type(exc).__name__)
                                try:collect_site(profile, client, records[i], candidate_cap=candidate_cap, page_cap=page_cap, search=search)
                                except Exception as exc:profile.error('website','Partial website extraction: '+type(exc).__name__)
                            if search and not nav_only:
                                try:search.enrich(profile,records[i],client)
                                except Exception as exc:profile.error('external_discovery','Optional source extraction: '+type(exc).__name__)
                            env = profile.finish(requests=budget.by_company[profile.org], runtime_ms=results[i]["operations"]["runtime_ms"] + (time.monotonic() - before) * 1000)
                            record = records[i]
                        else:
                            value = processor(item, run_id, sources, client, (seeds or {}).get(item["organisation_number"]),
                                        websites=False, candidate_cap=candidate_cap, page_cap=page_cap)
                            env, record = value if isinstance(value, tuple) else (value, None)
                        validate(env)
                    except Exception as exc:
                        if enrich:
                            import copy
                            env, record = copy.deepcopy(results[i]), records[i]
                            env["errors"].append({"stage": "discovery", "state": "failed", "message": "Company discovery failed: " + type(exc).__name__})
                        else:
                            env, record = failed_result(item, run_id, sources, f"Unhandled company error: {type(exc).__name__}: {str(exc)[:160]}", budget), None
                    done.put((i, env, record, client.attempts))
            for n in range(min(workers, len(indices))):
                threading.Thread(target=worker, name=f"{'web' if enrich else 'registry'}-{n}", daemon=True).start()
            received = 0
            try:
                while received < len(indices) and budget.clock() < phase_end:
                    try:
                        i, env, record, ledger = done.get(timeout=min(.2, max(.001, phase_end - budget.clock())))
                    except queue.Empty:
                        continue
                    results[i] = env
                    if record:
                        records[i] = record
                    if enrich:
                        discovery_done.add(i)
                    received += 1
                    ledger_write(env["organisation_number"], ledger)
                    # Every company completion is durably published, including its
                    # official facts before any optional service is contacted.
                    if received==1 or received==len(indices) or received%10==0 or time.monotonic()-last_checkpoint[0]>=1:
                        checkpoint();last_checkpoint[0]=time.monotonic()
                    if received % 25 == 0 or received == len(indices):
                        print(f"{'Discovery' if enrich else 'Registry'} {received}/{len(indices)}; outbound {budget.used}/{budget.limit}", flush=True,file=sys.stderr)
            finally:
                stop.set()
                checkpoint()
            # Late workers never write results or history themselves. Their sockets
            # are bounded, but the coordinator need not wait for a hung resolver.
        search = None
        try:
            phase(list(range(len(items))))
            if websites and records and budget.remaining() > 0:
                by_org = {items[i]["organisation_number"]: records[i] for i in records}
                env_by_org = {items[i]["organisation_number"]: results[i] for i in records}
                search = Serper(by_org, env_by_org, mode=search_mode, credits=paid_credits,
                                per_company=paid_per_company,public_search=public_search,spend_cap=spend_cap,
                                categories=paid_categories,website_rounds=website_paid_rounds)
                ranking = {org: rank for rank, org in enumerate(search.ranked)}
                indices = sorted(records, key=lambda i: (ranking.get(items[i]["organisation_number"], len(items)), i))
                phase(indices, enrich=True, search=search)
                if nav_pages and budget.remaining()>0:
                    # Early companies must see ads found later in the shared scan.
                    # Respect its own cutoff and leave time for detail verification.
                    while not nav.finished.is_set() and budget.remaining()>1 and budget.clock()<nav_client.deadline:
                        nav.finished.wait(min(.2,budget.remaining()))
                    phase(indices,enrich=True,nav_only=True)
        except KeyboardInterrupt:
            interrupted = True
            warnings.append("Interrupted; pending companies received explicit failed envelopes")
        finally:
            budget.stopped.set()
            if renderer:renderer.close()
        for i, item in enumerate(items):
            if i not in results:
                reason = "Interrupted by operator" if interrupted else "Batch deadline reached; company work incomplete"
                results[i] = failed_result(item, run_id, sources, reason, budget)
        ledger_write('shared_candidates',list(seed_client.attempts))
        if websites and nav_pages:ledger_write('shared_nav',list(nav_client.attempts))
    provider = search.report() if search else {"configured": False, "credits_used": 0, "third_party_cost_usd": 0.0}
    capture_integrity=finalize_captures(results,sources,items,run_id,budget)
    if capture_integrity['rejected']:
        warnings.append('Source integrity check rejected '+str(len(capture_integrity['rejected']))+' capture files; see capture_integrity')
    discovery_attempts=[{'organisation_number':e['organisation_number'],'strategy_version':STRATEGIES['owner_verification'],**a}
                        for e in results.values() for a in e.get('website_discovery',[])]
    crawl_attempts=[{'organisation_number':e['organisation_number'],**a}
                    for e in results.values() for a in e.get('crawl_attempts',[])]
    external_attempts=[{'organisation_number':e['organisation_number'],**a} for e in results.values() for a in e.get('external_discovery',[])]
    for i, raw_env in results.items():
        env=public_envelope(raw_env)
        results[i]=env
        org = env["organisation_number"]
        credits = search.by_company[org] if search else 0
        env["operations"]["paid_credits"] = credits
        if credits:
            env["operations"]["third_party_cost_usd"] = None if org in search.unknown_cost_companies else round(search.cost_by_company[org],6)
        if websites and i in records and i not in discovery_done:
            for claim in env["claims"]:
                if claim["field"] in {"official_website", "company_profile", "hiring_signal", "dated_activity"} and claim["availability"] != "available":
                    claim.update(availability="failed", reason="Discovery was not completed before the run stopped")
        try:
            refresh(env, snapshot_dir, captures=sources)
        except (OSError, ValueError) as exc:
            env["errors"].append({"stage": "snapshot", "state": "failed", "message": "Snapshot persistence failed: " + type(exc).__name__})
            warnings.append("A snapshot could not be preserved; inspect per-company errors")
    ordered = [results[i] for i in range(len(items))]
    body = "".join(canonical(env) + "\n" for env in ordered)
    atomic_write(journal, body)
    atomic_write(output / "submission.jsonl", body)
    atomic_write(output / "manifest.jsonl", "".join(canonical(item) + "\n" for item in items))
    fields = Counter()
    external_fields=Counter()
    states = Counter()
    for env in ordered:
        fields.update({c["field"] for c in env["claims"] if c["availability"] == "available"})
        evidence_by_id={e['id']:e for e in env['evidence']}
        external_fields.update({c['field'] for c in env['claims'] if c['availability']=='available' and not c.get('stale')
                                and any(not evidence_by_id[e]['source_class'].startswith('official_') for e in c['evidence_ids'])})
        states[env["run"]["terminal_status"]] += 1
    latencies = [env["operations"]["runtime_ms"] for env in ordered]
    project=Path(__file__).resolve().parents[1]
    runtime_files=[project/'run.py',*sorted((project/'signalpost').glob('*.py'))]
    runtime_digests={p.relative_to(project).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime_files}
    runtime_sha256=hashlib.sha256(canonical(runtime_digests).encode()).hexdigest()
    report = {"run_id": run_id, "input_count": len(items), "envelope_count": len(ordered),
              'runtime_sha256':runtime_sha256,'runtime_files':runtime_digests,
              'manifest_sha256':hashlib.sha256((''.join(canonical(i)+'\n' for i in items)).encode()).hexdigest(),
              'implementation_version':VERSION,'strategy_versions':STRATEGIES,'candidate_map':seed_report,
              'http_cache':{'enabled':bool(cache_dir),'fresh':fresh,'path':str(cache_dir) if cache_dir else None},
              'browser':renderer.report() if renderer else {'enabled':False},
              "one_result_per_input": len(ordered) == len(items), "terminal_states": dict(states),
              "requests_used": budget.used, "request_limit": budget.limit, "requests_by_host": dict(budget.by_host),
              "wall_clock_seconds": round(time.monotonic() - start, 3), "time_limit_seconds": max_seconds,
              "third_party_cost_usd": provider["third_party_cost_usd"], "provider": provider, "models": [], "api_keys_required": False,
              "nav": nav.report(), 'capture_integrity':capture_integrity,
              "company_coverage": dict(fields), "runtime_ms": {"p50": percentile(latencies, .5), "p95": percentile(latencies, .95)},
              'external_company_coverage':dict(external_fields),
              'shared_requests':{'nav':budget.by_company['shared_nav'],'candidate_map':budget.by_company['shared_candidates']},
              "deadline_reached": not interrupted and (budget.remaining()<=0 or any(any("deadline" in e.get("message", "").lower() for e in env["errors"]) for env in ordered)),
              "warnings": list(dict.fromkeys(warnings)), "official_score": False,
              "measurement_note": "Completion and observed coverage only; no official precision/recall score inferred"}
    atomic_write(output / "run-report.json", json.dumps(report, indent=2, ensure_ascii=False))
    atomic_write(output/'discovery-attempts.jsonl',''.join(canonical(a)+'\n' for a in discovery_attempts))
    atomic_write(output/'crawl-attempts.jsonl',''.join(canonical(a)+'\n' for a in crawl_attempts))
    atomic_write(output/'external-candidates.jsonl',''.join(canonical(a)+'\n' for a in external_attempts))
    atomic_write(output/'search-attempts.jsonl',''.join(canonical(a)+'\n' for a in (search.attempts if search else [])))
    return ordered, report
