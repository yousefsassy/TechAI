"""Opt-in rendering with retailer policy checks on every outbound resource."""
import time
from urllib.parse import urlsplit

from scraping.public_pages import USER_AGENT, SourceUnavailable, allowed_url, get_page, get_rules

_RESOURCE_TYPES = frozenset({'script', 'xhr', 'fetch'})
_STRIP_HEADERS = frozenset({'content-length', 'content-encoding', 'transfer-encoding', 'connection', 'set-cookie'})


def render(url, hostname, initial_page):
    from playwright.sync_api import sync_playwright
    from scrapling.parser import Selector

    root = 'https://' + hostname
    if not allowed_url(root, url) or initial_page.url != url or initial_page.status != 200:
        raise ValueError('browser_host_not_allowed')
    deadline = time.monotonic() + 25
    requests = 0
    unavailable = None

    def handle(route):
        nonlocal requests, unavailable
        request = route.request
        if request.method != 'GET' or unavailable or not allowed_url(root, request.url):
            route.abort()
            return
        try:
            rules = get_rules(root, deadline)
            if not rules.can_fetch(request.url, USER_AGENT):
                route.abort()
                return
            if request.resource_type == 'document' and request.url == url:
                response = initial_page
            elif request.resource_type in _RESOURCE_TYPES and requests < 6:
                requests += 1
                response = get_page(request.url, float(rules.crawl_delay(USER_AGENT) or 0), deadline)
            else:
                route.abort()
                return
        except SourceUnavailable as exc:
            unavailable = exc
            route.abort()
            return
        if response.status != 200:
            route.abort()
            return
        headers = {key: str(value) for key, value in response.headers.items() if key.lower() not in _STRIP_HEADERS}
        route.fulfill(status=200, headers=headers, body=response.body)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, service_workers='block')
            context.route_web_socket('**/*', lambda socket: socket.close())
            context.route('**/*', handle)
            page = context.new_page()
            response = page.goto(url, wait_until='domcontentloaded', timeout=25000)
            if unavailable:
                raise unavailable
            if response is None or response.status != 200 or urlsplit(page.url).hostname != hostname:
                raise ValueError('browser_response_unavailable')
            return Selector(page.content(), url=page.url)
        finally:
            browser.close()
