"""cTrader Open API connector.

Cross-platform (Linux, ChromeOS, macOS, Windows) — communicates with
cTrader's Open API over TCP using the ``ctrader-open-api`` package.

The cTrader Open API uses Twisted under the hood. To keep all public
functions synchronous (matching the broker_sdk contract), a single
Twisted reactor runs in a dedicated daemon thread started on first use.
Each API call dispatches work onto that thread via ``reactor.callFromThread``
and blocks the calling thread with a ``queue.Queue`` until the response
arrives or the timeout expires.

Install:  pip install ctrader-open-api

Credentials: ~/.vibe-trading/ctrader.json
    {
        "client_id":     "your_app_client_id",
        "client_secret": "your_app_client_secret",
        "access_token":  "your_account_access_token",
        "account_id":    12345678,
        "environment":   "demo"
    }

Getting credentials
-------------------
1. Register a free Open API app at https://connect.ctrader.com
   → copy client_id and client_secret.
2. Authorise your cTrader account via OAuth2 to obtain an access_token.
   The quickest way: cTrader Desktop → Settings → API → Generate token.
3. account_id: cTrader Desktop → Settings → Account info.

Supported environments: "demo" (paper trading) or "live".
"""

from __future__ import annotations

import json
import logging
import queue as _queue
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from src.config.paths import get_runtime_root

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "ctrader.json"

PROFILE_ENVIRONMENTS = {
    "demo": "paper",
    "live": "live",
}


class CTraderDependencyError(RuntimeError):
    """Raised when ``ctrader-open-api`` is not installed."""


class CTraderConfigError(RuntimeError):
    """Raised when credentials are missing or invalid."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


_CTRADER_TOKEN_URL = "https://connect.ctrader.com/oauth/token"

# In-memory cache: account_id → last refresh-check datetime (avoids checking every call).
_refresh_check_cache: dict[int, "datetime"] = {}


@dataclass(frozen=True)
class CTraderConfig:
    """cTrader connector credentials and settings.

    Attributes:
        client_id: Open API application client id (from connect.ctrader.com).
        client_secret: Open API application client secret.
        access_token: OAuth2 access token for the specific cTrader account.
        refresh_token: OAuth2 refresh token used to obtain new access tokens.
        token_expires_at: ISO-8601 UTC datetime when the access token expires.
        account_id: Numeric cTrader account id.
        environment: ``"demo"`` or ``"live"``.
        timeout: Per-request timeout in seconds.
    """

    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_expires_at: str = ""
    account_id: int = 0
    environment: str = "demo"
    timeout: int = 15


def _config_path() -> Path:
    return get_runtime_root() / CONFIG_FILENAME


def load_config() -> CTraderConfig:
    path = _config_path()
    if not path.exists():
        raise CTraderConfigError(
            f"cTrader config not found at {path}. "
            "Create ~/.vibe-trading/ctrader.json with client_id, client_secret, "
            "access_token, account_id, and environment."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return CTraderConfig(**{k: v for k, v in raw.items() if k in CTraderConfig.__dataclass_fields__})
    except Exception as exc:
        raise CTraderConfigError(f"Could not parse ctrader.json: {exc}") from exc


def save_config(config: CTraderConfig) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    return path


def refresh_access_token(config: CTraderConfig) -> CTraderConfig:
    """Exchange the stored refresh_token for a fresh access_token and persist it."""
    import urllib.parse
    import urllib.request

    from datetime import datetime, timedelta, timezone

    if not config.refresh_token:
        raise CTraderConfigError("No refresh_token in ctrader.json — cannot auto-refresh.")

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": config.refresh_token,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }).encode()

    req = urllib.request.Request(
        _CTRADER_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    expires_in = int(data.get("expires_in", 2628000))
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(timespec="seconds")

    new_config = CTraderConfig(
        client_id=config.client_id,
        client_secret=config.client_secret,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", config.refresh_token),
        token_expires_at=expires_at,
        account_id=config.account_id,
        environment=config.environment,
        timeout=config.timeout,
    )
    save_config(new_config)
    logger.info("[ctrader] Token refreshed — expires %s", expires_at)
    return new_config


def _maybe_refresh_token(config: CTraderConfig) -> CTraderConfig:
    """Return config with a fresh token if the current one expires within 7 days.

    Checks at most once per hour (in-memory cache) to avoid slowing every call.
    Falls back to the existing token on any error so the bot keeps running.
    """
    from datetime import datetime, timedelta, timezone

    if not config.refresh_token:
        return config

    now = datetime.now(timezone.utc)
    last_check = _refresh_check_cache.get(config.account_id)
    if last_check and (now - last_check).total_seconds() < 3600:
        return config  # already checked within the last hour

    _refresh_check_cache[config.account_id] = now

    should_refresh = False
    if not config.token_expires_at:
        should_refresh = True
    else:
        try:
            expires_at = datetime.fromisoformat(config.token_expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            days_left = (expires_at - now).days
            if days_left < 7:
                logger.info("[ctrader] Token expires in %d day(s) — refreshing", days_left)
                should_refresh = True
        except (ValueError, TypeError):
            should_refresh = True

    if not should_refresh:
        return config

    try:
        return refresh_access_token(config)
    except Exception as exc:
        logger.warning("[ctrader] Token auto-refresh failed: %s — using existing token", exc)
        return config


def build_config(profile_config: Mapping[str, Any], overrides: Mapping[str, Any] | None = None) -> CTraderConfig:
    stored = load_config()
    merged = {**asdict(stored), **(profile_config or {}), **(overrides or {})}
    return CTraderConfig(**{k: v for k, v in merged.items() if k in CTraderConfig.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Twisted reactor management
# ---------------------------------------------------------------------------

_reactor_lock = threading.Lock()
_reactor_thread: threading.Thread | None = None
_reactor_ready = threading.Event()


_twisted_observer_installed = False


def _install_twisted_log_filter() -> None:
    """Suppress harmless Twisted deferred-cleanup errors from cTrader connections."""
    global _twisted_observer_installed
    if _twisted_observer_installed:
        return
    _twisted_observer_installed = True
    try:
        import twisted.python.log as _tl

        _orig_err = _tl.err

        def _filtered_err(*args: Any, **kwargs: Any) -> None:
            try:
                failure = args[0] if args else kwargs.get("_failure") or kwargs.get("failure")
                type_name = getattr(getattr(failure, "type", None), "__name__", "")
                if type_name in ("TimeoutError", "CancelledError", "ConnectionDone", "ConnectionLost"):
                    return
            except Exception:
                pass
            _orig_err(*args, **kwargs)

        _tl.err = _filtered_err
    except Exception:
        pass


def _ensure_reactor() -> None:
    """Start the Twisted reactor in a daemon thread (idempotent)."""
    global _reactor_thread
    with _reactor_lock:
        if _reactor_thread is not None and _reactor_thread.is_alive():
            return

        _install_twisted_log_filter()

        def _run() -> None:
            try:
                from twisted.internet import reactor
                reactor.callLater(0, _reactor_ready.set)
                reactor.run(installSignalHandlers=False)
            except Exception as exc:
                logger.error("[ctrader] Reactor thread error: %s", exc)
                _reactor_ready.set()  # unblock callers even on failure

        _reactor_thread = threading.Thread(
            target=_run,
            name="ctrader-reactor",
            daemon=True,
        )
        _reactor_thread.start()
        _reactor_ready.wait(timeout=10)


# ---------------------------------------------------------------------------
# Core request executor
# ---------------------------------------------------------------------------


_RETRYABLE_PHRASES = ("disconnected before response", "timed out", "alreadyloggedin", "already_logged_in")

# cTrader demo server only allows ONE active application-auth session per
# client_id at a time. Opening connections in parallel or back-to-back
# causes ALREADYLOGGEDIN / disconnect errors. This lock serialises every
# API call so only one TCP connection is ever open at once.
_api_call_lock = threading.Lock()
_API_COOLDOWN_S = 3.0  # extra buffer (seconds) after confirmed TCP disconnect before next connection
_MAX_TIMEOUT_S = 60    # default timeout — large enough for ProtoOASymbolsListRes on slow demo servers


def _execute(
    config: CTraderConfig,
    make_request,
    *,
    timeout: int | None = None,
) -> Any:
    """Execute one cTrader API request using the auto-persistent session.

    The first call in a cycle opens a TCP connection and authenticates.
    All subsequent calls in the same cycle reuse that connection — no
    reconnecting, no per-call rate-limiting, no ALREADYLOGGEDIN errors.

    On timeout the session is intentionally kept alive.  The Pepperstone demo
    server sometimes responds 60-90 s after the request (large symbol list,
    server load).  Keeping the TCP connection open means the delayed response
    can satisfy the next retry on the same connection — no expensive
    reconnection and no rate-limit cascade.

    On hard disconnect / auth errors the session is invalidated so the next
    call reconnects cleanly.
    """
    config = _maybe_refresh_token(config)
    try:
        sess = _get_auto_session(config)
        return sess.execute(make_request, timeout=timeout)
    except TimeoutError:
        # Do NOT invalidate on timeout — connection is still open.
        # A delayed server response will be dropped harmlessly when it arrives.
        raise
    except Exception:
        _invalidate_auto_session()
        raise


def _execute_retrying(
    config: CTraderConfig,
    make_request: Any,
    *,
    timeout: int | None = None,
    max_retries: int = 3,
) -> Any:
    """Like ``_execute`` but retries on transient disconnect / timeout errors.

    ``_execute`` already holds a global lock and enforces a 2s cooldown after
    each attempt, so no extra sleep is needed between retries here.
    Non-retryable errors (permanent auth failures, cTrader error responses) are
    raised immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(max(1, max_retries)):
        if attempt > 0:
            logger.info(
                "[ctrader] Retry %d/%d — prev error: %s",
                attempt, max_retries - 1, last_exc,
            )
        try:
            return _execute(config, make_request, timeout=timeout)
        except (RuntimeError, TimeoutError) as exc:
            msg = str(exc).lower()
            if any(p in msg for p in _RETRYABLE_PHRASES):
                last_exc = exc
                continue
            raise
    raise last_exc  # type: ignore[misc]


def _handle_message(
    client: Any,
    message: Any,
    config: CTraderConfig,
    phase: list[str],
    client_holder: list[Any],
    make_request: Any,
    put_result: Any,
) -> None:
    """Route incoming messages based on auth phase."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAAccountAuthReq,
        ProtoOAApplicationAuthRes,
        ProtoOAAccountAuthRes,
        ProtoOAErrorRes,
    )
    from ctrader_open_api import Protobuf

    payload_type = message.payloadType

    # Error response — any phase.
    try:
        err_type = ProtoOAErrorRes().payloadType
        if payload_type == err_type:
            err = ProtoOAErrorRes()
            err.ParseFromString(message.payload)
            exc = RuntimeError(f"cTrader error {err.errorCode}: {err.description}")
            put_result(exc)
            _safe_stop(client)
            return
    except Exception:
        pass

    if phase[0] == "app_auth":
        try:
            app_res_type = ProtoOAApplicationAuthRes().payloadType
        except Exception:
            app_res_type = None

        if app_res_type is not None and payload_type == app_res_type:
            phase[0] = "account_auth"
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = config.account_id
            req.accessToken = config.access_token
            client.send(req)

    elif phase[0] == "account_auth":
        try:
            acc_res_type = ProtoOAAccountAuthRes().payloadType
        except Exception:
            acc_res_type = None

        if acc_res_type is not None and payload_type == acc_res_type:
            phase[0] = "request"
            make_request(client, message, put_result, _safe_stop)

    elif phase[0] == "request":
        # Delegate all subsequent messages to the active request handler.
        if hasattr(make_request, "on_message"):
            make_request.on_message(client, message, put_result, _safe_stop)


def _safe_stop(client: Any) -> None:
    try:
        client.stopService()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Persistent session — one TCP connection reused for all requests in a cycle
# ---------------------------------------------------------------------------


class CTraderSession:
    """Persistent authenticated cTrader TCP connection.

    Connects and authenticates ONCE.  Subsequent calls to ``execute()`` send
    requests over the same open socket, completely avoiding the per-connection
    rate-limit on cTrader demo servers (ALREADYLOGGEDIN / timeout cascade).

    Thread-safe: requests are serialised with an internal lock so only one
    request is in-flight at a time (the cTrader API is inherently sequential).

    Typically managed via the module-level auto-session helpers so callers
    don't need to change their code.
    """

    def __init__(self, config: CTraderConfig) -> None:
        self._config = config
        self._client: Any = None
        self._alive = False
        self._account_id = config.account_id
        self._lock = threading.Lock()
        self._current_rq: "_queue.Queue | None" = None
        self._current_handler: Any = None

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def account_id(self) -> int:
        return self._account_id

    def connect(self, timeout: int = 15) -> None:
        """Open TCP connection and authenticate once.  Blocks until ready."""
        config = self._config
        _check_dependency()
        _ensure_reactor()

        from ctrader_open_api import Client, TcpProtocol, EndPoints
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAApplicationAuthReq, ProtoOAApplicationAuthRes,
            ProtoOAAccountAuthReq, ProtoOAAccountAuthRes, ProtoOAErrorRes,
        )
        from twisted.internet import reactor as _reactor, defer as _defer

        phase: list[str] = ["connecting"]
        connect_q: _queue.Queue = _queue.Queue()

        def on_message(client: Any, message: Any) -> None:
            try:
                payload_type = message.payloadType
                try:
                    err_type = ProtoOAErrorRes().payloadType
                    if payload_type == err_type:
                        err = ProtoOAErrorRes()
                        err.ParseFromString(message.payload)
                        exc = RuntimeError(f"cTrader error {err.errorCode}: {err.description}")
                        if phase[0] in ("app_auth", "account_auth"):
                            connect_q.put(exc)
                        elif self._current_rq is not None:
                            self._current_rq.put(exc)
                        return
                except Exception:
                    pass

                if phase[0] == "app_auth":
                    if payload_type == ProtoOAApplicationAuthRes().payloadType:
                        phase[0] = "account_auth"
                        req = ProtoOAAccountAuthReq()
                        req.ctidTraderAccountId = config.account_id
                        req.accessToken = config.access_token
                        client.send(req)
                elif phase[0] == "account_auth":
                    if payload_type == ProtoOAAccountAuthRes().payloadType:
                        phase[0] = "ready"
                        self._alive = True
                        connect_q.put("ok")
                elif phase[0] == "ready":
                    rq = self._current_rq
                    h = self._current_handler
                    if rq is not None and h is not None and hasattr(h, "on_message"):
                        h.on_message(client, message, rq.put, lambda c: None)
            except Exception as exc:
                if self._current_rq is not None:
                    self._current_rq.put(exc)

        def on_connected(client: Any) -> None:
            _orig = client.send

            def _patched(*a: Any, **kw: Any) -> Any:
                d = _orig(*a, **kw)
                if d is not None:
                    try:
                        d.addErrback(lambda f: f.trap(_defer.TimeoutError, _defer.CancelledError))
                    except Exception:
                        pass
                return d

            client.send = _patched
            self._client = client
            phase[0] = "app_auth"
            req = ProtoOAApplicationAuthReq()
            req.clientId = config.client_id
            req.clientSecret = config.client_secret
            client.send(req)

        def on_disconnected(client: Any, reason: Any = None) -> None:
            self._alive = False
            if phase[0] in ("app_auth", "account_auth"):
                connect_q.put(RuntimeError("cTrader disconnected during auth"))
            elif self._current_rq is not None and self._current_rq.empty():
                self._current_rq.put(RuntimeError("cTrader session disconnected mid-request"))

        host = (
            EndPoints.PROTOBUF_DEMO_HOST if config.environment == "demo"
            else EndPoints.PROTOBUF_LIVE_HOST
        )
        ct_client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        ct_client.setConnectedCallback(on_connected)
        ct_client.setDisconnectedCallback(on_disconnected)
        ct_client.setMessageReceivedCallback(on_message)

        _reactor.callFromThread(ct_client.startService)

        try:
            result = connect_q.get(timeout=timeout)
        except _queue.Empty:
            raise TimeoutError(f"cTrader session auth timed out after {timeout}s")
        if isinstance(result, Exception):
            raise result

        logger.info("[ctrader] Persistent session open — account %d", config.account_id)

    def execute(self, make_request: Any, *, timeout: int | None = None) -> Any:
        """Send one request on the open connection and return the response."""
        if not self._alive:
            raise RuntimeError("cTrader session not connected")
        from twisted.internet import reactor as _reactor

        # Use explicit timeout if given; otherwise use _MAX_TIMEOUT_S.
        # Deliberately ignore self._config.timeout — ctrader.json may store an
        # old low value (e.g. 30) that would cap even explicit large timeouts.
        t = timeout if timeout is not None else _MAX_TIMEOUT_S

        with self._lock:
            rq: _queue.Queue = _queue.Queue()
            self._current_rq = rq
            self._current_handler = make_request

            def _send() -> None:
                try:
                    # auth_msg=None: already past auth phase on this connection.
                    # stop=no-op: do NOT close the connection after this request.
                    make_request(self._client, None, rq.put, lambda c: None)
                except Exception as exc:
                    rq.put(exc)

            _reactor.callFromThread(_send)
            try:
                value = rq.get(timeout=t)
            except _queue.Empty:
                raise TimeoutError(f"cTrader API call timed out after {t}s")
            finally:
                self._current_rq = None
                self._current_handler = None

        if isinstance(value, Exception):
            raise value
        return value

    def disconnect(self) -> None:
        """Close the persistent connection."""
        self._alive = False
        if self._client is not None:
            _safe_stop(self._client)
        logger.info("[ctrader] Persistent session closed — account %d", self._account_id)

    def __enter__(self) -> "CTraderSession":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.disconnect()


# Module-level auto-session: reused across all _execute calls for the same
# account within a single cycle.  Invalidated on any error so the next retry
# gets a fresh connection.
_auto_session: "CTraderSession | None" = None
_auto_session_lock = threading.Lock()


def _get_auto_session(config: CTraderConfig) -> "CTraderSession":
    """Return the current auto-session (creating one if needed)."""
    global _auto_session
    with _auto_session_lock:
        sess = _auto_session
        if sess is not None and sess.alive and sess.account_id == config.account_id:
            return sess
        # Need fresh session (first call or previous session died).
        if sess is not None:
            try:
                sess.disconnect()
            except Exception:
                pass
            # Brief pause so the demo server can release the previous session
            # before we open a new one (avoids implicit rate-limiting on rapid reconnects).
            import time as _t; _t.sleep(3)
        new_sess = CTraderSession(config)
        new_sess.connect(timeout=60)
        _auto_session = new_sess
        return new_sess


def _invalidate_auto_session() -> None:
    """Mark the current auto-session as dead so the next call reconnects."""
    global _auto_session
    with _auto_session_lock:
        if _auto_session is not None:
            _auto_session._alive = False
        _auto_session = None


def _execute_app_only(
    config: CTraderConfig,
    make_request: Any,
    *,
    timeout: int | None = None,
) -> Any:
    """Like _execute but skips account auth — used for account discovery."""
    config = _maybe_refresh_token(config)
    _check_dependency()
    _ensure_reactor()

    timeout = timeout or config.timeout
    result_q: _queue.Queue[Any] = _queue.Queue()

    def put_result(value: Any) -> None:
        result_q.put(value)

    def _start() -> None:
        try:
            from ctrader_open_api import Client, TcpProtocol, EndPoints
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAApplicationAuthReq,
                ProtoOAApplicationAuthRes,
                ProtoOAErrorRes,
            )

            host = (
                EndPoints.PROTOBUF_DEMO_HOST
                if config.environment == "demo"
                else EndPoints.PROTOBUF_LIVE_HOST
            )
            phase = ["connecting"]

            def on_message(client: Any, message: Any) -> None:
                try:
                    payload_type = message.payloadType
                    try:
                        err_type = ProtoOAErrorRes().payloadType
                        if payload_type == err_type:
                            err = ProtoOAErrorRes()
                            err.ParseFromString(message.payload)
                            result_q.put(RuntimeError(f"cTrader error {err.errorCode}: {err.description}"))
                            _safe_stop(client)
                            return
                    except Exception:
                        pass

                    if phase[0] == "app_auth":
                        if payload_type == ProtoOAApplicationAuthRes().payloadType:
                            phase[0] = "request"
                            make_request(client, message, put_result, _safe_stop)
                    elif phase[0] == "request":
                        if hasattr(make_request, "on_message"):
                            make_request.on_message(client, message, put_result, _safe_stop)
                except Exception as exc:
                    result_q.put(exc)
                    _safe_stop(client)

            def on_connected(client: Any) -> None:
                _orig_send = client.send

                def _patched_send(*a: Any, **kw: Any) -> Any:
                    d = _orig_send(*a, **kw)
                    if d is not None:
                        try:
                            from twisted.internet import defer as _defer
                            d.addErrback(
                                lambda f: f.trap(_defer.TimeoutError, _defer.CancelledError)
                            )
                        except Exception:
                            pass
                    return d

                client.send = _patched_send
                phase[0] = "app_auth"
                req = ProtoOAApplicationAuthReq()
                req.clientId = config.client_id
                req.clientSecret = config.client_secret
                client.send(req)

            def on_disconnected(client: Any, reason: Any = None) -> None:
                if result_q.empty():
                    result_q.put(RuntimeError("cTrader disconnected before response"))

            ct_client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
            ct_client.setConnectedCallback(on_connected)
            ct_client.setDisconnectedCallback(on_disconnected)
            ct_client.setMessageReceivedCallback(on_message)
            ct_client.startService()
        except Exception as exc:
            result_q.put(exc)

    from twisted.internet import reactor
    reactor.callFromThread(_start)

    try:
        value = result_q.get(timeout=timeout)
    except _queue.Empty:
        raise TimeoutError(f"cTrader API call timed out after {timeout}s")

    if isinstance(value, Exception):
        raise value
    return value


def _check_dependency() -> None:
    try:
        import ctrader_open_api  # noqa: F401
    except ImportError:
        raise CTraderDependencyError(
            "ctrader-open-api is not installed. "
            "Run: pip install ctrader-open-api"
        )


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _make_trader_request(client: Any, _auth_msg: Any, put_result: Any, stop: Any) -> None:
    """Request handler for get_account_snapshot."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOATraderReq, ProtoOATraderRes,
    )

    _res_type = ProtoOATraderRes().payloadType

    def on_message(c: Any, msg: Any, put: Any, stp: Any) -> None:
        if msg.payloadType == _res_type:
            res = ProtoOATraderRes()
            res.ParseFromString(msg.payload)
            put(res)
            stp(c)

    _make_trader_request.on_message = on_message  # type: ignore[attr-defined]
    req = ProtoOATraderReq()
    req.ctidTraderAccountId = client._config_account_id if hasattr(client, "_config_account_id") else 0
    client.send(req)


class _RequestHandler:
    """Base class for cTrader request handlers attached to make_request slot."""

    def __call__(self, client: Any, auth_msg: Any, put_result: Any, stop: Any) -> None:
        self._client = client
        self._put = put_result
        self._stop = stop
        self._send(client)

    def _send(self, client: Any) -> None:
        raise NotImplementedError

    def on_message(self, client: Any, msg: Any, put_result: Any, stop: Any) -> None:
        raise NotImplementedError


class _TraderRequest(_RequestHandler):
    def __init__(self, account_id: int) -> None:
        self._account_id = account_id

    def _send(self, client: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOATraderReq
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = self._account_id
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOATraderRes
        if msg.payloadType == ProtoOATraderRes().payloadType:
            res = ProtoOATraderRes()
            res.ParseFromString(msg.payload)
            put(res)
            stop(client)


class _ReconcileRequest(_RequestHandler):
    def __init__(self, account_id: int) -> None:
        self._account_id = account_id

    def _send(self, client: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileReq
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self._account_id
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileRes
        if msg.payloadType == ProtoOAReconcileRes().payloadType:
            res = ProtoOAReconcileRes()
            res.ParseFromString(msg.payload)
            put(res)
            stop(client)


class _SymbolsRequest(_RequestHandler):
    def __init__(self, account_id: int) -> None:
        self._account_id = account_id

    def _send(self, client: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolsListReq
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._account_id
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolsListRes
        if msg.payloadType == ProtoOASymbolsListRes().payloadType:
            res = ProtoOASymbolsListRes()
            res.ParseFromString(msg.payload)
            put(res)
            stop(client)


class _SpotRequest(_RequestHandler):
    """Subscribe to spots for one symbol, capture first tick, unsubscribe."""

    def __init__(self, account_id: int, symbol_id: int) -> None:
        self._account_id = account_id
        self._symbol_id = symbol_id

    def _send(self, client: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeSpotsReq
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId.append(self._symbol_id)
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASpotEvent
        if msg.payloadType == ProtoOASpotEvent().payloadType:
            evt = ProtoOASpotEvent()
            evt.ParseFromString(msg.payload)
            put(evt)
            stop(client)


class _TrendbarsRequest(_RequestHandler):
    def __init__(self, account_id: int, symbol_id: int, period: int, count: int) -> None:
        self._account_id = account_id
        self._symbol_id = symbol_id
        self._period = period
        self._count = count

    def _send(self, client: Any) -> None:
        import time
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsReq

        # fromTimestamp is a required proto2 field — compute from period + count.
        _period_ms = {
            1: 60_000, 2: 120_000, 3: 180_000, 4: 240_000, 5: 300_000,
            6: 600_000, 7: 900_000, 8: 1_800_000,
            9: 3_600_000, 10: 14_400_000, 11: 43_200_000,
            12: 86_400_000, 13: 604_800_000, 14: 2_592_000_000,
        }
        now_ms = int(time.time() * 1000)
        bar_ms = _period_ms.get(self._period, 3_600_000)

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = self._symbol_id
        req.period = self._period
        req.count = self._count
        req.fromTimestamp = now_ms - self._count * bar_ms
        req.toTimestamp = now_ms
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsRes
        if msg.payloadType == ProtoOAGetTrendbarsRes().payloadType:
            res = ProtoOAGetTrendbarsRes()
            res.ParseFromString(msg.payload)
            put(res)
            stop(client)


class _NewOrderRequest(_RequestHandler):
    def __init__(
        self,
        account_id: int,
        symbol_id: int,
        side: int,
        volume: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        self._account_id = account_id
        self._symbol_id = symbol_id
        self._side = side
        self._volume = volume
        self._stop_loss = stop_loss
        self._take_profit = take_profit

    def _send(self, client: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOANewOrderReq
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = self._symbol_id
        req.orderType = 1  # ProtoOAOrderType.MARKET
        req.tradeSide = self._side
        req.volume = self._volume
        req.comment = "vibe-trading"
        if self._stop_loss is not None:
            req.stopLoss = self._stop_loss
        if self._take_profit is not None:
            req.takeProfit = self._take_profit
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
        if msg.payloadType == ProtoOAExecutionEvent().payloadType:
            res = ProtoOAExecutionEvent()
            res.ParseFromString(msg.payload)
            put(res)
            stop(client)


class _AccountListRequest(_RequestHandler):
    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    def _send(self, client: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetAccountListByAccessTokenReq
        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = self._access_token
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetAccountListByAccessTokenRes
        if msg.payloadType == ProtoOAGetAccountListByAccessTokenRes().payloadType:
            res = ProtoOAGetAccountListByAccessTokenRes()
            res.ParseFromString(msg.payload)
            put(res)
            stop(client)


class _ClosePositionRequest(_RequestHandler):
    def __init__(self, account_id: int, position_id: int, volume: int) -> None:
        self._account_id = account_id
        self._position_id = position_id
        self._volume = volume

    def _send(self, client: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self._account_id
        req.positionId = self._position_id
        req.volume = self._volume
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
        if msg.payloadType == ProtoOAExecutionEvent().payloadType:
            res = ProtoOAExecutionEvent()
            res.ParseFromString(msg.payload)
            put(res)
            stop(client)


class _CancelOrderRequest(_RequestHandler):
    def __init__(self, account_id: int, order_id: int) -> None:
        self._account_id = account_id
        self._order_id = order_id

    def _send(self, client: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOACancelOrderReq
        req = ProtoOACancelOrderReq()
        req.ctidTraderAccountId = self._account_id
        req.orderId = self._order_id
        client.send(req)

    def on_message(self, client: Any, msg: Any, put: Any, stop: Any) -> None:
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
        if msg.payloadType == ProtoOAExecutionEvent().payloadType:
            res = ProtoOAExecutionEvent()
            res.ParseFromString(msg.payload)
            put(res)
            stop(client)


# ---------------------------------------------------------------------------
# Symbol cache (avoids repeated symbol list requests)
# ---------------------------------------------------------------------------

_symbol_cache_lock = threading.Lock()


def _load_symbol_cache_from_disk() -> "dict[str, dict[str, int]]":
    """Load persisted symbol→id mappings from disk (survives process restarts)."""
    path = get_runtime_root() / "ctrader_symbols.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            total = sum(len(v) for v in data.values())
            logger.info("[ctrader] Loaded symbol cache from disk (%d symbols)", total)
            return data
        except Exception as exc:
            logger.debug("[ctrader] Could not load symbol cache: %s", exc)
    return {}


def _save_symbol_cache_to_disk(cache: "dict[str, dict[str, int]]") -> None:
    """Persist symbol cache to disk so future restarts skip the heavy list fetch."""
    path = get_runtime_root() / "ctrader_symbols.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        logger.debug("[ctrader] Symbol cache saved to %s", path)
    except Exception as exc:
        logger.debug("[ctrader] Could not save symbol cache: %s", exc)


_symbol_cache: dict[str, dict[str, int]] = _load_symbol_cache_from_disk()  # env → {name_upper: symbolId}


_SYMBOL_LIST_TIMEOUT_S = 180  # ProtoOASymbolsListRes can be slow on Pepperstone demo


def _get_symbol_id(symbol: str, config: CTraderConfig) -> int:
    """Return the numeric symbolId for *symbol*, using a per-environment cache."""
    key = config.environment
    symbol_upper = symbol.strip().upper()

    with _symbol_cache_lock:
        if key in _symbol_cache and symbol_upper in _symbol_cache[key]:
            return _symbol_cache[key][symbol_upper]

    logger.warning(
        "[ctrader] Symbol cache empty — fetching full symbol list "
        "(may take up to %ds on Pepperstone demo). "
        "Run agent/scripts/fetch_symbol_ids.py once to pre-populate the cache and avoid this delay.",
        _SYMBOL_LIST_TIMEOUT_S,
    )
    res = _execute(config, _SymbolsRequest(config.account_id), timeout=_SYMBOL_LIST_TIMEOUT_S)
    mapping: dict[str, int] = {}
    for sym in res.symbol:
        name = sym.symbolName.strip().upper()
        mapping[name] = sym.symbolId

    with _symbol_cache_lock:
        _symbol_cache[key] = mapping
        _save_symbol_cache_to_disk(_symbol_cache)
        if symbol_upper not in mapping:
            raise ValueError(
                f"Symbol '{symbol}' not found on this cTrader account. "
                f"Available symbols: {', '.join(sorted(mapping)[:20])}..."
            )
        return mapping[symbol_upper]


# ---------------------------------------------------------------------------
# Period mapping
# ---------------------------------------------------------------------------

_PERIOD_MAP = {
    "1m": "M1", "m1": "M1",
    "5m": "M5", "m5": "M5",
    "15m": "M15", "m15": "M15",
    "30m": "M30", "m30": "M30",
    "1h": "H1", "h1": "H1",
    "4h": "H4", "h4": "H4",
    "1d": "D1", "d1": "D1",
    "1w": "W1", "w1": "W1",
    "1mo": "MN1", "mn1": "MN1",
}


_TRENDBAR_PERIOD_VALUES = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5,
    "M10": 6, "M15": 7, "M30": 8,
    "H1": 9, "H4": 10, "H12": 11,
    "D1": 12, "W1": 13, "MN1": 14,
}


def _period_to_ctrader(period: str) -> int:
    name = _PERIOD_MAP.get(period.lower(), "H1")
    return _TRENDBAR_PERIOD_VALUES.get(name, 9)  # default H1=9


# ---------------------------------------------------------------------------
# Public connector functions (broker_sdk contract)
# ---------------------------------------------------------------------------


def check_status(config: CTraderConfig | None = None) -> dict[str, Any]:
    if config is None:
        config = load_config()
    _check_dependency()
    try:
        res = _execute(config, _TraderRequest(config.account_id))
        return {
            "status": "connected",
            "environment": config.environment,
            "account_id": config.account_id,
            "balance": res.trader.balance / 100.0,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def list_accounts(config: CTraderConfig | None = None) -> list[dict[str, Any]]:
    """List all trading accounts linked to the access token.

    Returns the ctidTraderAccountId for each account. This is the correct
    ID to put in ctrader.json — NOT the broker account number shown in cTrader Desktop.
    """
    if config is None:
        config = load_config()
    _check_dependency()
    try:
        res = _execute_app_only(config, _AccountListRequest(config.access_token))
        accounts = []
        for acc in res.ctidTraderAccount:
            accounts.append({
                "ctid_trader_account_id": acc.ctidTraderAccountId,
                "is_live": acc.isLive,
                "trader_login": getattr(acc, "traderLogin", None),
            })
        return accounts
    except Exception as exc:
        return [{"error": str(exc)}]


def get_account_snapshot(config: CTraderConfig) -> dict[str, Any]:
    res = _execute(config, _TraderRequest(config.account_id))
    t = res.trader
    balance = t.balance / 100.0
    try:
        equity = t.equity / 100.0 if t.HasField("equity") else balance
    except (ValueError, AttributeError):
        equity = balance
    try:
        used_margin = t.marginUsed / 100.0 if t.HasField("marginUsed") else 0.0
    except (ValueError, AttributeError):
        used_margin = 0.0
    try:
        currency = t.depositAsset.name if t.HasField("depositAsset") else "USD"
    except (ValueError, AttributeError):
        currency = "USD"
    return {
        "account_id": config.account_id,
        "environment": config.environment,
        "balance": balance,
        "equity": equity,
        "used_margin": used_margin,
        "free_margin": equity - used_margin,
        "currency": currency,
    }


def get_positions(config: CTraderConfig) -> dict[str, Any]:
    res = _execute(config, _ReconcileRequest(config.account_id))
    positions = []
    for pos in res.position:
        side = "buy" if pos.tradeData.tradeSide == 1 else "sell"
        qty_lots = pos.tradeData.volume / 100000.0
        positions.append({
            "symbol": pos.tradeData.symbolId,  # numeric; resolved upstream if needed
            "quantity": qty_lots if side == "buy" else -qty_lots,
            "side": side,
            "entry_price": pos.price / 100000.0 if pos.price else 0,
            "position_id": pos.positionId,
        })
    return {"positions": positions}


def get_open_orders(config: CTraderConfig) -> dict[str, Any]:
    res = _execute(config, _ReconcileRequest(config.account_id))
    orders = []
    for order in res.order:
        orders.append({
            "order_id": order.orderId,
            "symbol": order.tradeData.symbolId,
            "side": "buy" if order.tradeData.tradeSide == 1 else "sell",
            "quantity": order.tradeData.volume / 100000.0,
            "order_type": order.orderType,
            "status": order.orderStatus,
        })
    return {"orders": orders}


def get_quote(symbol: str, config: CTraderConfig) -> dict[str, Any]:
    symbol_id = _get_symbol_id(symbol, config)
    evt = _execute_retrying(config, _SpotRequest(config.account_id, symbol_id), max_retries=2)
    bid = evt.bid / 100000.0 if evt.bid else 0.0
    ask = evt.ask / 100000.0 if evt.ask else 0.0
    mid = (bid + ask) / 2 if bid and ask else (bid or ask)
    if mid == 0.0:
        raise RuntimeError(f"cTrader quote for {symbol} returned bid=0/ask=0 (no tick yet)")
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "last": mid,
        "price": mid,
    }


def get_historical_bars(
    symbol: str,
    config: CTraderConfig,
    period: str = "1h",
    limit: int = 100,
) -> dict[str, Any]:
    symbol_id = _get_symbol_id(symbol, config)
    period_int = _period_to_ctrader(period)
    res = _execute_retrying(config, _TrendbarsRequest(config.account_id, symbol_id, period_int, limit), max_retries=2)
    bars = []
    for bar in res.trendbar:
        ts_s = bar.utcTimestampInMinutes * 60
        low = bar.low / 100000.0
        delta_open = bar.deltaOpen / 100000.0 if bar.HasField("deltaOpen") else 0.0
        delta_high = bar.deltaHigh / 100000.0 if bar.HasField("deltaHigh") else 0.0
        delta_close = bar.deltaClose / 100000.0 if bar.HasField("deltaClose") else 0.0
        open_p = low + delta_open
        high_p = low + delta_high
        close_p = low + delta_close
        bars.append({
            "timestamp": ts_s,
            "open": round(open_p, 5),
            "high": round(high_p, 5),
            "low": round(low, 5),
            "close": round(close_p, 5),
            "volume": bar.volume,
        })
    return {"bars": bars, "symbol": symbol, "period": period}


def place_order(
    config: CTraderConfig,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    order_type: str = "market",
    stop_loss: float | None = None,
    take_profit: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    qty = float(quantity or 0)
    symbol_id = _get_symbol_id(symbol, config)
    trade_side = 1 if side.lower() == "buy" else 2  # ProtoOATradeSide: BUY=1, SELL=2
    # qty from the signal engine = risk_notional / current_price = base-currency units
    # (e.g. 90.9 EUR for EURUSD).  cTrader volume is already in base-currency units
    # (1 standard lot = 100,000 units), so no multiplier needed.
    # Minimum 1,000 units (= 0.01 lots) to stay above broker minimums.
    volume = max(1000, int(round(qty)))

    logger.info(
        "[ctrader] Placing %s %s: symbolId=%s vol=%d sl=%s tp=%s",
        side.upper(), symbol, symbol_id, volume, stop_loss, take_profit,
    )

    res = _execute(
        config,
        _NewOrderRequest(config.account_id, symbol_id, trade_side, volume, stop_loss, take_profit),
    )

    # ProtoOAExecutionType: ORDER_FILLED=3, ORDER_ACCEPTED=2, ORDER_REJECTED=7
    exec_type = res.executionType
    if exec_type == 7:  # ORDER_REJECTED
        error_code = ""
        try:
            error_code = res.errorCode or ""
        except Exception:
            pass
        raise RuntimeError(
            f"cTrader rejected order {symbol} {side} vol={volume} sl={stop_loss} tp={take_profit}: {error_code or 'ORDER_REJECTED'}"
        )

    position_id = res.position.positionId if res.HasField("position") else None
    if exec_type not in (2, 3, 11) and position_id is None:
        logger.warning(
            "[ctrader] Unexpected executionType=%s for %s %s — no position created",
            exec_type, side, symbol,
        )

    result: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "volume": volume,
        "execution_type": exec_type,
        "order_id": res.order.orderId if res.HasField("order") else None,
        "position_id": position_id,
        "status": "placed" if position_id else "accepted",
    }
    if stop_loss is not None:
        result["stop_loss"] = stop_loss
    if take_profit is not None:
        result["take_profit"] = take_profit
    return result


def close_position(
    config: CTraderConfig,
    *,
    position_id: int,
    volume: int,
    **kwargs: Any,
) -> dict[str, Any]:
    res = _execute(config, _ClosePositionRequest(config.account_id, int(position_id), int(volume)))
    return {
        "position_id": position_id,
        "status": "closed",
        "execution_type": res.executionType,
    }


def cancel_order(order_id: str, config: CTraderConfig, **kwargs: Any) -> dict[str, Any]:
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid cTrader order_id: {order_id!r} (must be numeric)")

    res = _execute(config, _CancelOrderRequest(config.account_id, oid))
    return {
        "order_id": order_id,
        "status": "cancelled",
        "execution_type": res.executionType,
    }
