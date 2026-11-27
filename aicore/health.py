"""aicore.health — shared /health endpoint for every app built on aicore.

THE RULE: every aicore app MUST expose a health endpoint.  One line at app
startup wires it up — nothing to configure but ``APP_ID`` in your ``.env``:

    from aicore import add_health_endpoint
    add_health_endpoint(app)

or (router style):

    from aicore import health_router
    app.include_router(health_router())

What the check does — and nothing else
--------------------------------------
1. app — if THIS endpoint responds, the app process is up.  (trivially ok)
2. LLM hosts — a plain reachability probe (HTTP GET) to the URL of every
   model the app uses.  NO API key, NO content generation, NO database ping.

Which URLs?  Exactly the ones the app uses, read from the DB: the saved query
``aicore_app_provider_urls`` (param ``app_id``) returns ``MODEL_PROVIDER``
and ``URL`` of every ACTIVE model of the app's ACTIVE tasks.  Rows without a
URL are ignored.  If the app has no URL at all there is nothing to check and
the endpoint simply answers ``healthy``.  Each distinct host is probed once
(results cached 60 s so frequent polling doesn't hammer the API).

The app id comes from ``APP_ID`` in ``.env`` (or ``app_id=`` explicitly);
if it is missing an ``AppIdMissingError`` is raised — at startup, when you
call ``add_health_endpoint`` / ``health_router``.

ANY HTTP status (200/404/405...) counts as *reachable* — the point is the
network path.  Connect error / timeout / DNS / SSL failure → UNHEALTHY (503).
If the lookup itself fails (DB API down) the answer is UNHEALTHY with the
reason in ``detail``.

Response (200 when healthy, 503 when not):

    {
      "status": "healthy" | "unhealthy",
      "checks": {
        "app": {"status": "ok"},
        "llm": [                      # empty list = nothing to check
          {"provider": "ollama", "url": "http://<ollama-host>:11434",
           "reachable": true, "http_status": 200, "latency_ms": 12, "detail": null}
        ]
      }
    }

Lower-level pieces (import from ``aicore`` or ``aicore.health``):

    health_payload(app_id=None)               → dict (the body above)
    check_reachability(url, timeout=...)      → dict (raw probe)
    app_provider_urls(app_id)                 → ([(provider, url)], error)

No new dependencies — the probe uses the stdlib ``urllib``.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

from .env import get_app_id

DEFAULT_TIMEOUT = 5.0   # seconds — a health check must never hang a LB poll

URLS_QUERY = "aicore_app_provider_urls"   # saved DB-API query, param: app_id
_CACHE_TTL = 60.0                         # seconds; /health is polled often

_db_cache: dict = {}   # app_id -> (monotonic_ts, [(provider, url)], error)


def _origin(url: str) -> str:
    from urllib.parse import urlparse
    u = urlparse(url.strip())
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else url.strip()


def app_provider_urls(app_id, refresh: bool = False):
    """→ ``([(provider, url), ...], error)`` for the models ``app_id`` uses.

    Runs the saved DB-API query ``aicore_app_provider_urls`` (columns
    ``MODEL_PROVIDER``, ``URL``).  Rows with an empty URL are dropped and
    each distinct host appears once.  Cached 60 s.  ``error`` is None on
    success, else the reason the lookup failed (and the list is empty)."""
    now = time.monotonic()
    hit = _db_cache.get(app_id)
    if hit and not refresh and now - hit[0] < _CACHE_TTL:
        return hit[1], hit[2]
    found: list = []
    err = None
    try:
        from .dbapi import query
        seen = set()
        for r in query(URLS_QUERY, app_id=app_id).dicts():
            r = {str(k).upper(): v for k, v in r.items()}
            url = str(r.get("URL") or "").strip()
            if not url:
                continue
            prov = str(r.get("MODEL_PROVIDER") or "").strip().lower() or "unknown"
            key = (prov, _origin(url))
            if key not in seen:
                seen.add(key)
                found.append(key)
    except Exception as e:                       # DB/API down → report, don't crash
        found, err = [], f"{type(e).__name__}: {e}"
    _db_cache[app_id] = (now, found, err)
    return found, err


def _urllib_probe(url: str, timeout: float, verify: bool) -> dict:
    import ipaddress
    import ssl
    from urllib.parse import urlparse

    ctx = (ssl.create_default_context() if verify
           else ssl._create_unverified_context())

    # Internal/private hosts (e.g. the Ollama server, loopback) must be probed
    # DIRECTLY — a corporate http_proxy configured machine-wide would
    # otherwise intercept them and answer 403 itself, masking a dead host.
    host = (urlparse(url).hostname or "")
    direct = host in ("localhost",) or any(
        host.startswith(pfx) for pfx in ("127.", "10.", "172.", "169.254."))
    if not direct:
        try:
            direct = ipaddress.ip_address(host).is_private
        except ValueError:
            pass
    if direct:
        handlers = [urllib.request.ProxyHandler({})]   # no corporate proxy
    else:
        handlers = []
    if url.lower().startswith("https"):
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)

    res: dict = {
        "url": url, "reachable": False, "http_status": None,
        "latency_ms": None, "detail": None,
    }
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        with opener.open(req, timeout=timeout) as resp:
            res["reachable"] = True
            res["http_status"] = (resp.status if hasattr(resp, "status")
                                  else resp.getcode())
    except urllib.error.HTTPError as e:      # a server answered ⇒ route works
        res["reachable"] = True
        res["http_status"] = e.code
    except Exception as e:                   # URLError / timeout / SSL / DNS …
        res["detail"] = f"{type(e).__name__}: {e}".rstrip(": ")
    res["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return res


def check_reachability(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fire one HTTP GET at ``url`` and report reachability.

    Any HTTP response (even 4xx/5xx) ⇒ reachable: the network path from the
    server to the host works.  Connect errors, timeouts, DNS/SSL failures
    ⇒ unreachable, with the error in ``detail``.

    Corporate MITM proxies intercept TLS with their own cert:
    if the ONLY failure is an untrusted certificate, the probe retries once
    unverified and still counts the host as reachable (noted in ``detail``).
    """
    res = _urllib_probe(url, timeout, verify=True)
    if not res["reachable"] and "CERTIFICATE_VERIFY_FAILED" in (res["detail"] or ""):
        res2 = _urllib_probe(url, timeout, verify=False)
        if res2["reachable"]:
            res2["detail"] = ("TLS cert untrusted (likely proxy MITM) — "
                              "reachable, probe retried without verification")
            res = res2
    return res


def health_payload(app_id=None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Build the full health payload (see module docstring for the shape).

    ``app_id`` defaults to ``APP_ID`` from ``.env``; raises
    ``AppIdMissingError`` if neither is available."""
    app_id = get_app_id(app_id)
    targets, err = app_provider_urls(app_id)

    llm: list = []
    if err:                                       # couldn't find out what to check
        llm.append({"provider": None, "url": None, "reachable": False,
                    "http_status": None, "latency_ms": None,
                    "detail": f"lookup of app {app_id}'s model URLs failed: {err}"})
    elif targets:                                 # probe all URLs concurrently
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
            probes = list(ex.map(lambda t: check_reachability(t[1], timeout),
                                 targets))
        llm = [{"provider": p, **pr} for (p, _), pr in zip(targets, probes)]

    healthy = all(e["reachable"] for e in llm)    # no URLs → nothing to check
    return {
        "status": "healthy" if healthy else "unhealthy",
        "checks": {
            "app": {"status": "ok", "detail": "app is up and responding"},
            "llm": llm,
        },
    }


# ---------------------------------------------------------------------------
# Wiring: one-liners that put the /health route on any FastAPI/Starlette app
# ---------------------------------------------------------------------------

def _make_handler(payload_fn):
    def _handler(request=None):   # request accepted: FastAPI's APIRoute passes it
        body = payload_fn()
        code = 200 if body["status"] == "healthy" else 503
        import json
        try:                                         # FastAPI style
            from fastapi import Response
            return Response(content=json.dumps(body), status_code=code,
                            media_type="application/json")
        except Exception:                            # plain starlette style
            from starlette.responses import JSONResponse
            return JSONResponse(content=body, status_code=code)
    return _handler


def add_health_endpoint(
    app,
    app_id=None,
    path: str = "/health",
    timeout: float = DEFAULT_TIMEOUT,
):
    """Add the shared ``GET /health`` route to a FastAPI/Starlette app.

    Call this ONCE after creating the app — every aicore app must expose it.
    ``app_id`` defaults to ``APP_ID`` from ``.env`` and is checked right here,
    so a missing id fails at startup (``AppIdMissingError``).  The endpoint
    is deliberately NOT wrapped with ``@ai_history``: health probes must work
    without the 5 audit fields and must not pollute history.
    """
    app_id = get_app_id(app_id)
    handler = _make_handler(lambda: health_payload(app_id=app_id, timeout=timeout))
    if hasattr(app, "add_route"):                    # FastAPI + Starlette
        app.add_route(path, handler, methods=["GET"])
    elif hasattr(app, "get"):                        # FastAPI .get() form
        app.get(path)(handler)
    return handler


def health_router(
    app_id=None,
    path: str = "/health",
    timeout: float = DEFAULT_TIMEOUT,
):
    """Same check, as a router: ``app.include_router(health_router())``."""
    app_id = get_app_id(app_id)
    handler = _make_handler(lambda: health_payload(app_id=app_id, timeout=timeout))
    try:
        from fastapi import APIRouter
    except ImportError:
        from starlette.routing import APIRouter
    router = APIRouter()
    router.add_api_route(path, handler, methods=["GET"])
    return router


__all__ = [
    "URLS_QUERY",
    "add_health_endpoint",
    "app_provider_urls",
    "check_reachability",
    "health_payload",
    "health_router",
]
