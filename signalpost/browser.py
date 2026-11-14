"""Optional JS-shell rendering. All page HTTP goes through the bounded Client."""
from __future__ import annotations
import queue
import re
import threading
from .net import Response, clean_url, utcnow


def needs_render(page):
    return len(page.root.text())<250 and bool(re.search(r'<script\b',page.raw,re.I)) and bool(re.search(r'id=[\"\'](?:root|app|__next)|__NUXT__|enable javascript|javascript is required',page.raw,re.I))


class BrowserRenderer:
    def __init__(self,max_renders=25,page_seconds=10,request_cap=16):
        self.max_renders,self.page_seconds,self.request_cap=max_renders,page_seconds,request_cap
        self.jobs=queue.Queue();self.lock=threading.Lock();self.thread=None
        self.renders=0;self.failures=0;self.requests=0;self.unavailable=None

    def render_if_needed(self,page,client):
        if not needs_render(page) or self.unavailable:return None
        with self.lock:
            if self.renders>=self.max_renders:return None
            self.renders+=1
            if self.thread is None:
                self.thread=threading.Thread(target=self._worker,daemon=True,name='browser-renderer');self.thread.start()
        done=queue.Queue(maxsize=1);self.jobs.put((page,client,done))
        wait=min(self.page_seconds+3,max(.01,client.deadline-client.budget.clock()))
        try:return done.get(timeout=wait)
        except queue.Empty:return None

    def _worker(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as runtime:
                options={'headless':True,'timeout':4000,'args':['--disable-background-networking','--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp']}
                try:browser=runtime.chromium.launch(**options)
                except Exception:browser=runtime.chromium.launch(channel='msedge',**options)
                try:
                    while True:
                        job=self.jobs.get()
                        if job is None:return
                        page,client,done=job
                        if client.budget.clock()>=client.deadline:done.put(None);continue
                        context=None
                        try:
                            context=browser.new_context(service_workers='block',accept_downloads=False)
                            if hasattr(context,'route_web_socket'):context.route_web_socket('**/*',lambda ws:ws.close())
                            count=0
                            def intercept(route):
                                nonlocal count
                                request=route.request
                                if request.method!='GET' or request.resource_type in {'image','media','font','stylesheet'} or count>=self.request_cap:
                                    route.abort();return
                                url=clean_url(request.url)
                                if not url or client.budget.clock()>=client.deadline:route.abort();return
                                count+=1
                                result=page.response if url==page.response.url else client.get(url,retries=0)
                                if result.status!=200:route.abort();return
                                # Avoid browser-managed redirects: Client already validates every hop.
                                headers={k:v for k,v in (result.headers or {}).items() if k.lower() not in {'content-length','content-encoding','transfer-encoding','set-cookie','location'}}
                                route.fulfill(status=200,body=result.body,headers=headers)
                            context.route('**/*',intercept)
                            tab=context.new_page()
                            timeout=min(self.page_seconds,max(.1,client.deadline-client.budget.clock()))*1000
                            tab.goto(page.response.url,wait_until='domcontentloaded',timeout=timeout)
                            try:tab.wait_for_load_state('networkidle',timeout=min(1500,timeout))
                            except Exception:pass
                            html=tab.content().encode('utf-8')
                            result=Response(tab.url,200,html,{'content-type':'text/html; charset=utf-8'},utcnow(),state='available',
                                            requested_url=page.response.requested_url or page.response.url,
                                            redirect_chain=page.response.redirect_chain,extraction_method='browser_rendered_dom',derived_from=page.response.digest)
                            client.capture(result)
                            with self.lock:self.requests+=count
                            done.put(result)
                        except Exception:
                            with self.lock:self.failures+=1
                            done.put(None)
                        finally:
                            if context:context.close()
                finally:browser.close()
        except Exception as exc:
            self.unavailable=type(exc).__name__+': optional Playwright/browser unavailable'
            while True:
                try:
                    job=self.jobs.get_nowait()
                    if job is not None:job[2].put(None)
                except queue.Empty:return

    def close(self):
        if self.thread:self.jobs.put(None)

    def report(self):
        return {'enabled':True,'render_attempts':self.renders,'failures':self.failures,'routed_resources':self.requests,
                'unavailable':self.unavailable,'max_renders':self.max_renders,'request_cap_per_page':self.request_cap}
