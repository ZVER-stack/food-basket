"""Anonymous storefront JSON through the user-launched dedicated local Chrome."""
import json
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import sync_playwright, Error as PlaywrightError

import ozon_fresh_fast as core
import ozon_har_catalog as oh


class BrowserTransport:
    def __init__(self, cdp="http://127.0.0.1:9223", interval=1.05, seconds=7200,
                 max_requests=6800, state_path="work/browser_transport_state.json",
                 isolated_context=False, stop_event=None, product_mode='navigation',page_url=None,backoff_gate=None,
                 allow_duplicate_test_tabs=False):
        if interval < 1:
            raise core.ParseError("At least one second between requests is required")
        self.interval = interval
        self.base_interval = interval
        self.deadline = time.monotonic() + seconds
        self.max_requests = max_requests
        self.count = 0
        self.state_path = Path(state_path)
        self.stop_event = stop_event
        self.backoff_gate = backoff_gate
        self.owned_context = None
        self.product_mode = product_mode
        if product_mode not in ('navigation','api'):
            raise core.ParseError('Unknown product transport mode')
        self.state = core.load_json(self.state_path) if self.state_path.exists() else {}
        self.interval = max(self.interval, self.state.get("min_interval", 1))
        if self.state.get("blocked"):
            if backoff_gate:
                reason=self.state.get('reason','Previous protection response')
                manual=any(x in reason.casefold() for x in ('captcha','капч','challenge','authenticated','manual access'))
                # A resumed reader can carry the same already-accounted-for
                # failure. Preserve its cooldown/manual signal without counting
                # a second HTTP failure that never occurred.
                if self.state.get('blocked_at_unix',0)>getattr(backoff_gate,'last_failure_unix',0):
                    backoff_gate.penalize(reason,manual=manual,observed_at=self.state.get('blocked_at_unix'))
                backoff_gate.clear_after_wait(self)
            else:raise core.ProtectionError("This transport was stopped after a protection response")
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.connect_over_cdp(cdp, timeout=10000)
            if isolated_context:
                self.check()
                self.owned_context = self.browser.new_context(locale='ru-RU',viewport={'width':1440,'height':900})
                self.page = self.owned_context.new_page()
                # Bootstrap HTML contains the widgets. Do not load assets or
                # storefront scripts that would issue uncontrolled background APIs.
                self.page.route('**/*',lambda route:route.continue_() if route.request.resource_type in ('document','fetch') else route.abort())
                delay=self.state.get('last_request_unix',0)+self.interval-time.time()
                if delay>0:time.sleep(delay)
                self.check()
                self.state['last_request_unix']=time.time()
                self.state['requests']=self.state.get('requests',0)+1
                self.count+=1
                core.write_json(self.state_path,self.state)
                response=self.page.goto('https://www.ozon.ru/category/supermarket-25000/?miniapp=supermarket',wait_until='domcontentloaded',timeout=30000)
                status=response.status if response else 0
                self.state['last_status']=status
                core.write_json(self.state_path,self.state)
                if status in (0,401,403,429):self.stop(f'Worker public home HTTP {status}')
                if status!=200:raise core.ParseError(f'Worker public home HTTP {status}')
                if urlsplit(self.page.url).hostname not in ('ozon.ru','www.ozon.ru'):
                    self.stop('Worker public home redirected outside Ozon')
            else:
                pages = [p for c in self.browser.contexts for p in c.pages
                         if urlsplit(p.url).hostname in ("www.ozon.ru", "ozon.ru")]
                if page_url is not None:
                    core.public_url(page_url)
                    pages=[p for p in pages if urlsplit(p.url).path==urlsplit(page_url).path]
                if (allow_duplicate_test_tabs and len(pages)>1 and
                    all(urlsplit(p.url).path.rstrip('/').endswith('-2952307571') for p in pages)):
                    owners=[c for c in self.browser.contexts if any(p in c.pages for p in pages)]
                    if len(owners)==1:pages=[pages[-1]]
                if len(pages) != 1:
                    raise core.ParseError("Expected exactly one dedicated anonymous Fresh tab")
                self.page = pages[0]
            title = self.page.title().casefold()
            if any(word in title for word in ("antibot", "captcha", "доступ ограничен")):
                self.stop("Challenge page is open")
            if 'нет соединения' in title:
                self.stop('Storefront unavailable; manual access check required')
            embedded = oh.document_states(self.page.content())
            if not any(k.startswith("profileMenuAnonymous") for k in oh.widgets(embedded)):
                raise core.ParseError("Anonymous profile is not confirmed in the storefront HTML")
        except PlaywrightError:
            if self.owned_context:
                self.owned_context.close()
            self.pw.stop()
            raise core.ParseError("Cannot read the dedicated Chrome connection; leave its anonymous Fresh tab open") from None
        except Exception:
            if self.owned_context:
                self.owned_context.close()
            self.pw.stop()
            raise

    def close(self):
        # Disconnect only; don't close the user's Chrome window.
        try:
            if self.owned_context:
                self.owned_context.close()
        finally:
            self.pw.stop()

    def stop(self, reason):
        self.state["blocked"] = True
        self.state["blocked_at_unix"] = time.time()
        self.state["reason"] = reason
        core.write_json(self.state_path, self.state)
        if getattr(self,'stop_event',None) is not None and not getattr(self,'backoff_gate',None):
            self.stop_event.set()
        raise core.ProtectionError(reason)

    @staticmethod
    def prepare_retry_after_pause(state_path, interval=3.0, cooldown=120,manual_access_confirmed=False):
        """Explicit new check after a pause; never called automatically on a 403."""
        state = core.load_json(state_path)
        if not state.get("blocked"):
            return
        if state.get("explicit_checks_after_pause", 0) >= 2 and not manual_access_confirmed:
            raise core.ProtectionError("Access remained restricted after increased pauses; stop and check the public storefront manually")
        previous = state.get("blocked_at_unix", state.get("last_request_unix", time.time()))
        if time.time() - previous < cooldown or interval < 3:
            raise core.ProtectionError("Keep the run stopped until the increased pause has elapsed")
        state["previous_block_reason"] = state.get("reason")
        state.pop('reason',None)
        state["blocked"] = False
        if manual_access_confirmed:state['explicit_manual_access_confirmations']=state.get('explicit_manual_access_confirmations',0)+1
        state["min_interval"] = max(interval, state.get("min_interval", 1))
        state["explicit_checks_after_pause"] = state.get("explicit_checks_after_pause", 0) + 1
        core.write_json(state_path, state)

    def check(self):
        if getattr(self,'stop_event',None) is not None and self.stop_event.is_set():
            raise core.BudgetError('Parallel run stopped by another worker or the shared deadline')
        if self.state.get("blocked"):
            raise core.ProtectionError(self.state.get("reason", "Transport stopped"))
        if self.count >= self.max_requests or time.monotonic() >= self.deadline:
            raise core.BudgetError("Browser request/time budget exhausted")

    def _pause(self, seconds):
        event=getattr(self,'stop_event',None)
        if event is None:time.sleep(seconds)
        elif event.wait(seconds):raise core.BudgetError('Collection interrupted or deadline reached')

    def request(self, spec, values=None):
        gate=getattr(self,'backoff_gate',None)
        while True:
            if gate:
                gate.wait();self.interval=max(getattr(self,'base_interval',3),gate.interval)
            try:
                result=self._request_once(spec,values)
                if gate:gate.note_success()
                return result
            except core.ProtectionError as exc:
                if not gate:raise
                reason=str(exc)
                status=self.state.get('last_status')
                manual=any(word in reason.casefold() for word in ('captcha','капч','challenge','authenticated','manual access'))
                if status not in (403,429) and not manual:raise
                gate.penalize(reason,manual=manual,retry_after=self.state.get('retry_after_seconds',0))
                gate.clear_after_wait(self)

    @staticmethod
    def _has_challenge_url(text):
        # The storefront can return a JSON challenge instead of CAPTCHA HTML.
        # Its top-level challengeURL is a manual gate, including on HTTP200.
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            return False
        return isinstance(body, dict) and any(
            key.casefold() == 'challengeurl' and isinstance(value, str) and bool(value.strip())
            for key, value in body.items())

    def _request_once(self, spec, values=None):
        self.check()
        spec = core.render(spec, values or {})
        url = core.public_url(spec["url"])
        document = spec.get("public_product_document") is True
        if not urlsplit(url).path.startswith("/api/") and not (document and urlsplit(url).path.startswith("/product/")):
            raise core.ParseError("Only public storefront API paths are allowed")
        method = spec.get("method", "GET").upper()
        if method not in ("GET", "POST"):
            raise core.ParseError("Unsupported storefront request method")
        delay = self.state.get("last_request_unix", 0) + self.interval - time.time()
        if delay > 0:
            self._pause(delay)
        gate=getattr(self,'backoff_gate',None)
        if gate:
            gate.wait();self.interval=max(getattr(self,'base_interval',3),gate.interval)
        self.check()
        self.state["last_request_unix"] = time.time()
        self.state["requests"] = self.state.get("requests", 0) + 1
        self.count += 1
        core.write_json(self.state_path, self.state)
        payload = {"url": url, "method": method, "json": spec.get("json"), "document": document,
                   "timeout": int(min(30000, max(1, self.deadline-time.monotonic())*1000))}
        try:
            result = self.page.evaluate("""async spec => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), spec.timeout);
            try {
                const options = {method: spec.method, credentials:'include', redirect:'manual',
                    signal:controller.signal, headers:spec.document ? {accept:'text/html'} : {accept:'application/json','x-o3-app-name':'dweb_client'}};
                if (spec.json !== null && spec.json !== undefined) {
                    options.body=JSON.stringify(spec.json); options.headers['content-type']='application/json';
                }
                const r=await fetch(spec.url, options);
                return {status:r.status, type:r.headers.get('content-type'), retryAfter:r.headers.get('retry-after'), text:await r.text()};
            } finally { clearTimeout(timer); }
            }""", payload)
        except PlaywrightError:
            raise core.ParseError("Browser request failed or timed out; no automatic retry") from None
        self.state["last_status"] = result["status"]
        self.state['retry_after_seconds']=0
        if result.get('retryAfter'):
            from email.utils import parsedate_to_datetime
            try:self.state['retry_after_seconds']=max(0,float(result['retryAfter']))
            except (ValueError,TypeError):
                try:self.state['retry_after_seconds']=max(0,parsedate_to_datetime(result['retryAfter']).timestamp()-time.time())
                except (ValueError,TypeError,OverflowError):pass
        core.write_json(self.state_path, self.state)
        if self._has_challenge_url(result.get('text')):
            self.stop(f"Challenge URL in HTTP {result['status']}; manual access required")
        if result["status"] in (0, 401, 403, 429) or 300 <= result["status"] < 400:
            if any(word in result.get('text','')[:20000].casefold() for word in ('captcha','капча')):
                self.stop(f"CAPTCHA in HTTP {result['status']}; manual access required")
            if 'нет соединения' in result.get('text','')[:20000].casefold():
                self.stop(f"Storefront unavailable in HTTP {result['status']}; manual access check required")
            self.stop(f"HTTP {result['status']}; increasing shared cooldown" if getattr(self,'backoff_gate',None)
                      else f"HTTP {result['status']} or redirect; stopped without retry")
        if result["status"] != 200:
            raise core.ParseError(f"HTTP {result['status']}; no zone marked complete")
        if document:
            if "html" not in (result["type"] or "").casefold():
                raise core.ParseError("Expected a public product HTML document")
            if any(w in result["text"][:20000].casefold() for w in core.BLOCK_WORDS):
                self.stop("Product document returned a challenge page")
            data = oh.document_states(result["text"])
            if not data["widgetStates"]:
                raise core.ParseError("Product HTML contained no embedded storefront widgets")
            if any(k.startswith("profileMenu") and not k.startswith("profileMenuAnonymous") for k in data["widgetStates"]):
                self.stop("Authenticated profile detected; anonymous access only")
            return data
        if "json" not in (result["type"] or "").casefold():
            if any(w in result["text"][:20000].casefold() for w in core.BLOCK_WORDS):
                self.stop("Storefront returned a challenge page")
            raise core.ParseError("Expected storefront JSON")
        try:
            data = json.loads(result["text"])
        except ValueError:
            raise core.ParseError("Invalid storefront JSON") from None
        if not isinstance(data, dict):
            raise core.ParseError("Storefront returned a non-object JSON response")
        states=data.get('widgetStates',{})
        if not isinstance(states,dict):raise core.ParseError('Storefront widgetStates is not an object')
        # Price/address/offer checks inspect the same widgets several times.
        # Decode each serialized widget once per response, rather than on each
        # check; this does not change the anonymous session or request rate.
        data['widgetStates']={key:oh.decode(value) for key,value in states.items()}
        for key in data['widgetStates']:
            if key.startswith("profileMenu") and not key.startswith("profileMenuAnonymous"):
                self.stop("Authenticated profile detected; anonymous access only")
        return data

    def entrypoint(self, path, body=None):
        url = core.API + core.urllib.parse.quote(path, safe="")
        spec = {"url": url}
        if body is not None:
            spec.update(method="POST", json=body)
        return self.request(spec)

    def product_document(self, published_link):
        return self.request({"url": core.public_url(published_link), "public_product_document": True})

    def product_page(self, published_link):
        """Normal product navigation first, matching the original parser's workflow.

        Uses the same anonymous context and stopped-state guard. A protection
        response stops immediately, before any follow-up API call.
        """
        self.check()
        url = core.public_url(published_link)
        if not urlsplit(url).path.startswith('/product/'):
            raise core.ParseError('Expected a public product URL')
        if getattr(self,'product_mode','navigation') == 'api':
            parts=urlsplit(url)
            path=parts.path+('?' + parts.query if parts.query else '')
            return self.entrypoint(path)
        delay = self.state.get('last_request_unix', 0) + self.interval - time.time()
        if delay > 0:
            self._pause(delay)
        self.check()
        self.state['last_request_unix'] = time.time()
        self.state['requests'] = self.state.get('requests', 0) + 1
        self.count += 1
        core.write_json(self.state_path, self.state)
        # The original parser navigates the existing tab. Keep that workflow;
        # creating a new tab is a different navigation context.
        tab = self.page
        try:
            response = tab.goto(url, wait_until='domcontentloaded',
                timeout=int(min(30000, max(1, self.deadline-time.monotonic())*1000)))
            status = response.status if response else 0
            self.state['last_status'] = status
            core.write_json(self.state_path, self.state)
            if status in (0, 401, 403, 429):
                self.stop(f'Product navigation HTTP {status}; no API retry')
            if status != 200:
                raise core.ParseError(f'Product navigation HTTP {status}')
            actual = urlsplit(tab.url)
            if actual.hostname not in ('www.ozon.ru', 'ozon.ru') or actual.path != urlsplit(url).path:
                raise core.ParseError('Product navigation redirected away from the requested item')
            if any(word in tab.title().casefold() for word in ('antibot', 'captcha', 'доступ ограничен')):
                self.stop('Product navigation returned a challenge')
            data = oh.document_states(tab.content())
            if any(key.startswith('profileMenu') and not key.startswith('profileMenuAnonymous') for key in oh.widgets(data)):
                self.stop('Authenticated product profile detected')
            if any(key.startswith('webPrice-') for key in oh.widgets(data)):
                return data
            # If the rendered document lacks the price, request JSON from that
            # product tab, with the same global request pause and budget.
            return self.entrypoint(urlsplit(url).path)
        except PlaywrightError:
            raise core.ParseError('Product navigation failed; no automatic retry') from None
