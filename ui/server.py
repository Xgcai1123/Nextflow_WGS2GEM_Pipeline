#!/usr/bin/env python3
"""HTTP layer for the local Web UI (contract: docs/ui.md sections 3, 7-10).

Run via ``bin/pipeline-ui`` (which resolves python3 and ``exec``s this file):

    bin/pipeline-ui --project DIR [--nextflow PATH] [--token TOK] [--no-open]

Responsibilities kept here, and nothing else:
  * single-instance ``flock`` and ``server.json`` (attach information),
  * token / Origin / cookie authentication for 127.0.0.1 only,
  * SSE log replay from a byte offset over the append-only ``run.log``,
  * streaming downloads (report/log with Content-Length; results as a
    streaming tar.gz -- 435 MB never enters memory, UI-D9),
  * static files for the single-page frontend (``ui/index.html`` etc.).

Everything about a run lives in :mod:`runner`; everything about the sample
table lives in :mod:`sheet`.
"""

import argparse
import codecs
import fcntl
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import runner
import sheet

UI_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_FILES = {
    "/app.js": "app.js",
    "/style.css": "style.css",
}
MAX_BODY_BYTES = 1024 * 1024          # contract: POST body limit 1 MiB
REPORT_TAG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
FASTA_SUFFIXES = (".fasta", ".fa", ".fna")
DEFAULT_TOKEN_COOKIE = "ui_token"


class ApiError(Exception):
    def __init__(self, status, code, message, detail=None, extra=None):
        Exception.__init__(self, message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail
        self.extra = extra or {}

    def payload(self):
        body = {"error": self.code, "message": self.message}
        if self.detail is not None:
            body["detail"] = self.detail
        body.update(self.extra)
        return body


class UIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class UIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NextFlow-WGS2GEM-ui/1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        # Quiet by default: run-ui-check greps stdout/stderr and a per-request
        # access log would drown the useful lines.  UI_DEBUG=1 restores it.
        if os.environ.get("UI_DEBUG"):
            sys.stderr.write("[ui] %s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query, keep_blank_values=True)
            if not self._origin_ok():
                raise ApiError(403, "forbidden",
                               "Origin header not allowed (only http://127.0.0.1:<port> "
                               "or http://localhost:<port>)")
            if path in STATIC_FILES and method == "GET":
                self._serve_static(path)
                return
            if path == "/" and method == "GET":
                self._serve_index(query)
                return
            if path.startswith("/api/"):
                if not self._token_ok(query):
                    raise ApiError(403, "forbidden",
                                   "Missing or incorrect token; open the full URL printed by the launcher")
                if method == "GET":
                    self._route_get(path, query)
                else:
                    self._route_post(path, query)
                return
            raise ApiError(404, "not_found", "Unknown path: %s" % path)
        except ApiError as exc:
            if method == "POST" and exc.status >= 400:
                # A POST may have been rejected before its body was consumed;
                # keeping the connection alive would desynchronise the next
                # request (its bytes would be read as this request's body).
                self.close_connection = True
            self._send_json(exc.status, exc.payload())
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # never let a handler die silently mid-request
            try:
                sys.stderr.write("[ui] internal error on %s %s: %r\n"
                                 % (method, self.path, exc))
                self._send_json(500, {
                    "error": "internal_error",
                    "message": "Internal server error: %s" % exc,
                })
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    # -- auth -------------------------------------------------------------
    def _token_ok(self, query):
        expected = self.server.token
        candidates = []
        tokens = query.get("token") or []
        if tokens:
            candidates.append(tokens[0])
        header = self.headers.get("X-UI-Token")
        if header:
            candidates.append(header.strip())
        cookie = self._cookie_value(DEFAULT_TOKEN_COOKIE)
        if cookie:
            candidates.append(cookie)
        expected_bytes = expected.encode("utf-8", "replace")
        for candidate in candidates:
            if candidate and hmac.compare_digest(
                    candidate.encode("utf-8", "replace"), expected_bytes):
                return True
        return False

    def _cookie_value(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.strip() == name:
                return value.strip()
        return None

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.port
        allowed = {
            "http://127.0.0.1:%d" % port,
            "http://localhost:%d" % port,
        }
        return origin in allowed

    # -- responses --------------------------------------------------------
    def _send_bytes(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, _header_value(value))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_html(self, status, body, extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._send_bytes(status, body, "text/html; charset=utf-8",
                         extra_headers)

    # -- pages / static ---------------------------------------------------
    def _serve_index(self, query):
        expected = self.server.token
        query_token = (query.get("token") or [None])[0]
        cookie_ok = self._cookie_value(DEFAULT_TOKEN_COOKIE) == expected
        if query_token != expected and not cookie_ok:
            page = (
                "<!DOCTYPE html><html lang=\"en\"><head>"
                "<meta charset=\"utf-8\"><title>403 - NextFlow-WGS2GEM</title>"
                "</head><body><h1>403 Unauthorized</h1>"
                "<p>Open this page with the full URL (including the token) printed by the launcher.</p>"
                "</body></html>"
            )
            self._send_html(403, page)
            return
        index_path = os.path.join(UI_DIR, "index.html")
        try:
            with open(index_path, "rb") as fh:
                body = fh.read()
        except OSError:
            page = (
                "<!DOCTYPE html><html lang=\"en\"><head>"
                "<meta charset=\"utf-8\"><title>NextFlow-WGS2GEM Web UI</title>"
                "</head><body><h1>Frontend assets missing</h1>"
                "<p>Cannot find <code>ui/index.html</code>; "
                "the API is still available -- restore the frontend files and reload.</p>"
                "</body></html>"
            )
            self._send_html(503, page)
            return
        headers = {}
        if query_token == expected:
            headers["Set-Cookie"] = (
                "%s=%s; Path=/; HttpOnly; SameSite=Strict"
                % (DEFAULT_TOKEN_COOKIE, expected))
        self._send_html(200, body, headers)

    def _serve_static(self, path):
        name = STATIC_FILES[path]
        full = os.path.join(UI_DIR, name)
        content_type = ("text/javascript; charset=utf-8"
                        if name.endswith(".js") else "text/css; charset=utf-8")
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            raise ApiError(404, "not_found", "Static asset not found: %s" % name)
        self._send_bytes(200, body, content_type)

    # -- routing ----------------------------------------------------------
    def _route_get(self, path, query):
        manager = self.server.manager
        if path == "/api/state":
            self._send_json(200, {
                "server": self._server_info(),
                "run": manager.snapshot(),
            })
        elif path == "/api/preflight":
            self._send_json(200, manager.preflight())
        elif path == "/api/events":
            self._serve_events(query)
        elif path == "/api/fs":
            self._send_json(200, self._fs_listing(query))
        elif path == "/api/report":
            self._serve_report()
        elif path == "/api/download":
            self._serve_download(query)
        else:
            raise ApiError(404, "not_found", "Unknown API endpoint: %s" % path)

    def _route_post(self, path, query):
        manager = self.server.manager
        if path == "/api/run":
            self._handle_run()
        elif path == "/api/cancel":
            self._drain_body()
            try:
                manager.request_cancel()
            except runner.NoActiveRunError as exc:
                raise ApiError(409, exc.code, exc.message)
            self._send_json(200, {"cancel_requested": True})
        else:
            raise ApiError(404, "not_found", "Unknown API endpoint: %s" % path)

    def _server_info(self):
        return {
            "project": self.server.project,
            "host": self.server.host,
            "port": self.server.port,
            "url": self.server.url,
            "pid": os.getpid(),
            "started_at": self.server.started_at,
            "non_loopback": self.server.non_loopback,
            "nextflow": self.server.manager.nextflow or None,
        }

    # -- POST /api/run ----------------------------------------------------
    def _handle_run(self):
        manager = self.server.manager
        # Busy is checked before body validation: while a run is active the
        # contract answer is 409, whatever the payload looks like.
        if manager.is_running():
            raise ApiError(409, "busy", "A run is already in progress",
                           extra={"run": manager.snapshot()})
        external = manager.external_nextflow_pids()
        if external:
            raise ApiError(
                409, "busy",
                "Detected an external nextflow process (pid %s); stop it first"
                % ", ".join(str(p) for p in external),
                extra={"run": manager.snapshot()})
        body = self._read_json_body()

        mode = body.get("mode", "fresh")
        if mode not in ("fresh", "resume"):
            raise ApiError(400, "invalid_field",
                           "mode must be fresh or resume", {"field": "mode"})
        profile = body.get("profile", "standard")
        if profile not in ("standard", "lowmem"):
            raise ApiError(400, "invalid_field",
                           "profile must be standard or lowmem",
                           {"field": "profile"})
        clean = body.get("clean", False)
        if not isinstance(clean, bool):
            raise ApiError(400, "invalid_field", "clean must be a boolean",
                           {"field": "clean"})
        report_tag = body.get("report_tag", "") or ""
        if not isinstance(report_tag, str):
            raise ApiError(400, "invalid_field", "report_tag must be a string",
                           {"field": "report_tag"})
        if report_tag and not REPORT_TAG_RE.match(report_tag):
            raise ApiError(400, "invalid_field",
                           "report_tag may only contain letters, digits, dots, underscores and hyphens",
                           {"field": "report_tag"})

        samples = body.get("samples")
        validation = sheet.validate_samples(samples)
        if validation["errors"]:
            # contract section 6: row+field+code errors are top-level
            raise ApiError(400, "validation_failed", "Sample sheet validation failed",
                           extra={"errors": validation["errors"]})
        try:
            run = manager.start(samples, mode, clean, profile, report_tag)
        except runner.NextflowMissingError as exc:
            raise ApiError(409, exc.code, exc.message,
                           extra={"run": exc.run})
        except runner.BusyError as exc:
            raise ApiError(409, exc.code, exc.message,
                           extra={"run": exc.run})
        result = {"run_id": run.get("id")}
        if validation["warnings"]:
            result["warnings"] = validation["warnings"]
        self._send_json(202, result)

    def _drain_body(self, limit=65536):
        """Consume a small request body so the connection stays in sync."""
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw)
        except (TypeError, ValueError):
            return
        if length <= 0:
            return
        if length > limit:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except OSError:
            self.close_connection = True

    def _read_json_body(self):
        content_type = (self.headers.get("Content-Type") or "").split(";")[0]
        content_type = content_type.strip().lower()
        if content_type != "application/json":
            raise ApiError(400, "bad_content_type",
                           "POST only accepts application/json")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ApiError(400, "bad_request", "Missing a valid Content-Length")
        if length > MAX_BODY_BYTES:
            # Drain a bounded prefix before answering: replying while the client
            # is still writing would turn the 413 into a broken pipe.  Then close
            # the connection, since the remaining request bytes cannot be trusted.
            remaining = min(length, MAX_BODY_BYTES + 65536)
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            self.close_connection = True
            raise ApiError(413, "body_too_large", "Request body exceeds 1 MiB")
        if length <= 0:
            raise ApiError(400, "invalid_json", "Request body is empty")
        data = self.rfile.read(length)
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(400, "invalid_json", "Request body is not valid JSON: %s" % exc)
        if not isinstance(parsed, dict):
            raise ApiError(400, "invalid_json", "Request body must be a JSON object")
        return parsed

    # -- GET /api/fs ------------------------------------------------------
    def _fs_listing(self, query):
        manager = self.server.manager
        requested = (query.get("path") or [None])[0]
        if not requested:
            requested = manager.params.get("raw_dir") or self.server.project
        requested = os.path.expanduser(str(requested))
        if not os.path.isabs(requested):
            raise ApiError(400, "not_a_directory", "path must be an absolute path",
                           {"path": requested})
        real = os.path.realpath(requested)
        if not os.path.isdir(real):
            raise ApiError(400, "not_a_directory", "Not a directory: %s" % real,
                           {"path": real})
        try:
            names = sorted(os.listdir(real), key=lambda s: s.lower())
        except OSError as exc:
            raise ApiError(400, "not_a_directory", "Cannot read directory: %s" % exc,
                           {"path": real})
        dirs = []
        files = []
        for name in names:
            full = os.path.join(real, name)
            try:
                if os.path.isdir(full):
                    dirs.append({"name": name, "path": full})
                elif os.path.isfile(full):
                    kind = _pick_kind(name)
                    if kind is None:
                        continue
                    files.append({
                        "name": name,
                        "path": full,
                        "size": os.path.getsize(full),
                        "kind": kind,
                    })
            except OSError:
                continue
        parent = os.path.dirname(real)
        if parent == real:
            parent = None
        shortcuts = []
        for label, key in (("raw", "raw_dir"), ("reference", "ref_dir")):
            value = manager.params.get(key)
            if value:
                shortcuts.append({"name": label, "path": str(value)})
        return {
            "path": real,
            "parent": parent,
            "home": os.path.expanduser("~"),
            "shortcuts": shortcuts,
            "dirs": dirs,
            "files": files,
        }

    # -- report / downloads ----------------------------------------------
    def _serve_report(self):
        report = self.server.manager.report_path()
        path = (report or {}).get("path")
        if not path or not os.path.isfile(path):
            raise ApiError(404, "no_report", "No MultiQC report located yet")
        self._stream_file(path, "text/html; charset=utf-8",
                          extra_headers={"X-UI-Report-Path": _header_value(path)})

    def _serve_download(self, query):
        manager = self.server.manager
        what = (query.get("what") or [None])[0]
        if what == "results":
            self._stream_results_tar()
        elif what == "report":
            report = manager.report_path()
            path = (report or {}).get("path")
            if not path or not os.path.isfile(path):
                raise ApiError(404, "no_report", "No MultiQC report located yet")
            self._stream_file(path, "text/html; charset=utf-8",
                              download_name=os.path.basename(path),
                              extra_headers={"X-UI-Report-Path": _header_value(path)})
        elif what == "log":
            path = manager.log_path()
            if not path or not os.path.isfile(path):
                raise ApiError(404, "no_log", "No run log yet")
            self._stream_file(path, "text/plain; charset=utf-8",
                              download_name="run.log")
        else:
            raise ApiError(400, "bad_what",
                           "what must be results, report or log")

    def _stream_file(self, path, content_type, download_name=None,
                     extra_headers=None):
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise ApiError(404, "not_found", "File does not exist: %s" % exc)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"'
                             % _header_filename(download_name))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, _header_value(value))
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _stream_results_tar(self):
        """Stream ``results/`` as a tar.gz straight into the socket.

        No Content-Length on purpose: the archive size is not known before the
        last byte, and buffering a 435 MB tree (UI-D9) is exactly what the
        contract forbids.  ``Connection: close`` delimits the body instead, and
        ``tarfile``'s stream mode (``w|gz``) feeds the compressor chunk by
        chunk into ``wfile`` -- nothing is staged on disk either.
        """
        manager = self.server.manager
        root = manager.results_root()
        if not os.path.isdir(root):
            raise ApiError(404, "no_results", "Results directory does not exist: %s" % root)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = "results-%s.tar.gz" % stamp
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def _filter(member):
            # contract: no .DS_Store; symlinks are stored as links (tarfile
            # never follows them in add()).
            if os.path.basename(member.name) == ".DS_Store":
                return None
            return member

        try:
            with tarfile.open(fileobj=self.wfile, mode="w|gz") as archive:
                archive.add(
                    root,
                    arcname=os.path.basename(root) or "results",
                    recursive=True,
                    filter=_filter,
                )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:
            sys.stderr.write("[ui] results tar stream failed: %r\n" % exc)

    # -- SSE --------------------------------------------------------------
    def _serve_events(self, query):
        manager = self.server.manager
        raw_from = (query.get("from") or [None])[0]
        last_event = self.headers.get("Last-Event-ID")
        source = raw_from if raw_from is not None else last_event
        try:
            offset = max(0, int(source)) if source else 0
        except (TypeError, ValueError):
            offset = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        current_run_id = None
        emitted_state = None
        last_ping = time.time()
        while True:
            run = manager.snapshot()
            run_id = (run or {}).get("id")
            # Push a state event when progress moves too, not only when the state
            # name changes: otherwise an open page sees "running" once and the
            # counters/ladder freeze (they would only be rescued by the 5 s poll;
            # the user's "the Run module does not update" report, F51). 26 tasks
            # produce 26 extra ~1 KB events per run, which is nothing.
            tasks = (run or {}).get("tasks") or {}
            state_key = (run_id, (run or {}).get("state"),
                         tasks.get("submitted"), tasks.get("completed"),
                         tasks.get("failed"))
            if state_key != emitted_state:
                self._sse_write("state",
                                json.dumps({"run": run}, ensure_ascii=False))
                emitted_state = state_key
            if current_run_id is None:
                current_run_id = run_id
            elif run_id != current_run_id:
                # New run: run.log is a new file, offsets from the previous one
                # are meaningless.  The initial ?from= only applies to the run
                # that was current when the connection was opened.
                current_run_id = run_id
                offset = 0
                decoder = codecs.getincrementaldecoder("utf-8")("replace")

            log_path = manager.log_path()
            if log_path:
                try:
                    size = os.path.getsize(log_path)
                except OSError:
                    size = 0
                if size < offset:      # defensive: never seek backwards forever
                    offset = 0
                if size > offset:
                    with open(log_path, "rb") as fh:
                        fh.seek(offset)
                        data = fh.read(size - offset)
                    text = decoder.decode(data)
                    if text:
                        self._sse_write(
                            "log",
                            json.dumps({"from": offset, "text": text},
                                       ensure_ascii=False))
                    offset = size
            now = time.time()
            if now - last_ping >= 15.0:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                last_ping = now
            else:
                self.wfile.flush()
            time.sleep(0.25)

    def _sse_write(self, event, data):
        payload = "event: %s\ndata: %s\n\n" % (event, data)
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pick_kind(name):
    low = name.lower()
    if low.endswith(FASTQ_SUFFIXES):
        return "fastq"
    if low.endswith(FASTA_SUFFIXES):
        return "fasta"
    return None


def _header_filename(name):
    safe = re.sub(r'[^A-Za-z0-9._-]', "_", name)
    return safe or "download"


def _header_value(value):
    """HTTP headers are latin-1 on the wire; never let a path raise here."""
    return str(value).encode("latin-1", "replace").decode("latin-1")


def _display_host(host):
    if host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return host


def _url_host(host):
    display = _display_host(host)
    if ":" in display and not display.startswith("["):
        return "[%s]" % display
    return display


def _build_url(host, port, token):
    return "http://%s:%d/?token=%s" % (_url_host(host), port, token)


def _port_in_use(host, port):
    target = host
    if host in ("0.0.0.0", "::", "", "localhost"):
        target = "127.0.0.1"
    try:
        conn = socket.create_connection((target, port), timeout=0.25)
    except OSError:
        return False
    try:
        conn.close()
    except OSError:
        pass
    return True


def _create_http_server(host, requested_port):
    candidates = [0] if requested_port == 0 else list(
        range(requested_port, requested_port + 20))
    last_error = None
    for candidate in candidates:
        if candidate != 0 and _port_in_use(host, candidate):
            continue
        try:
            httpd = UIHTTPServer((host, candidate), UIHandler)
        except OSError as exc:
            last_error = exc
            continue
        return httpd
    raise SystemExit(
        "pipeline-ui: cannot bind a port on %s (requested %s, tried 20): %s"
        % (host, requested_port, last_error))


def _read_server_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_server_json(path, payload):
    try:
        runner.atomic_write_json(path, payload)
    except OSError:
        pass


def _acquire_lock(ui_dir):
    os.makedirs(ui_dir, mode=0o700, exist_ok=True)
    lock_path = os.path.join(ui_dir, "server.lock")
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        existing = _read_server_json(os.path.join(ui_dir, "server.json"))
        payload = {
            "url": existing.get("url"),
            "port": existing.get("port"),
            "token": existing.get("token"),
            "pid": existing.get("pid"),
        }
        print("UI_ALREADY_RUNNING " + json.dumps(payload, ensure_ascii=False))
        sys.stdout.flush()
        try:
            lock_fh.close()
        except OSError:
            pass
        raise SystemExit(1)
    return lock_fh


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="pipeline-ui",
        description="NextFlow-WGS2GEM local Web UI (single instance, single task, 127.0.0.1)",
    )
    parser.add_argument("--project", default=os.getcwd(),
                        help="project root; must contain main.nf + nextflow.config (default: current directory)")
    parser.add_argument("--port", type=int, default=8787,
                        help="listen port (default 8787; if busy, +1 up to 20 tries; 0 = system-assigned)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="listen address (default 127.0.0.1; non-loopback prints a warning)")
    parser.add_argument("--nextflow", default=None,
                        help="nextflow executable; by default looks on PATH, then "
                             "~/miniforge3/envs/nextflow/bin/nextflow")
    parser.add_argument("--token", default=None,
                        help="fixed token (for tests); random by default")
    parser.add_argument("--no-open", action="store_true", default=False,
                        help="do not open the browser automatically")
    parser.add_argument("--print-url", action="store_true", default=False,
                        help="after a successful start, print one UI_READY {json} line to stdout, then block")
    parser.add_argument("--nextflow-arg", action="append", default=[],
                        metavar="ARG",
                        help="repeatable; appended to the end of the nextflow run command line (testing/advanced use)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if not str(args.project or "").strip():
        # An explicit empty --project must not silently mean "the cwd": that is
        # how a test/client typo writes runtime state into the real checkout.
        sys.stderr.write("pipeline-ui: --project must not be empty\n")
        return 1
    project = os.path.abspath(os.path.expanduser(args.project))
    for required in ("main.nf", "nextflow.config"):
        if not os.path.isfile(os.path.join(project, required)):
            sys.stderr.write(
                "pipeline-ui: %s is not a project root (missing %s)\n"
                % (project, required))
            return 1

    ui_dir = os.path.join(project, ".nextflow", "ui")
    lock_fh = _acquire_lock(ui_dir)

    token = args.token or secrets.token_urlsafe(16)
    host = args.host or "127.0.0.1"
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback:
        sys.stderr.write(
            "pipeline-ui: warning: --host %s is not a loopback address; it is reachable on the LAN, "
            "and this UI only protects a local single user -- do not use it on an untrusted network\n" % host)

    nextflow = runner.resolve_nextflow(args.nextflow)
    params, config_error = runner.read_flat_config(
        project, nextflow, args.nextflow_arg)
    if config_error:
        sys.stderr.write("pipeline-ui: %s (using built-in default paths)\n" % config_error)

    manager = runner.RunManager(
        project=project, nextflow=nextflow, extra_args=args.nextflow_arg,
        params=params, ui_dir=ui_dir)

    httpd = _create_http_server(host, args.port)
    port = httpd.server_address[1]
    httpd.port = port
    httpd.host = host
    httpd.project = project
    httpd.token = token
    httpd.manager = manager
    httpd.started_at = time.time()
    httpd.non_loopback = not loopback
    httpd.url = _build_url(host, port, token)

    manager.load()

    server_json_path = os.path.join(ui_dir, "server.json")
    server_json = {
        "port": port,
        "host": host,
        "url": httpd.url,
        "token": token,
        "pid": os.getpid(),
        "started_at": httpd.started_at,
        "project": project,
        "non_loopback": httpd.non_loopback,
        "nextflow": nextflow or None,
        "alive": True,
    }
    _write_server_json(server_json_path, server_json)

    if args.print_url:
        print("UI_READY " + json.dumps({
            "url": httpd.url,
            "port": port,
            "host": host,
            "token": token,
            "pid": os.getpid(),
        }, ensure_ascii=False))
        sys.stdout.flush()
    else:
        print("NextFlow-WGS2GEM Web UI")
        print("  project:  %s" % project)
        print("  nextflow: %s" % (nextflow or "(not found)"))
        print("  URL:      %s" % httpd.url)
        print("  press Ctrl-C to quit (an active run is cancelled on exit)")
        sys.stdout.flush()

    if not args.no_open:
        if sys.platform == "darwin" and shutil.which("open"):
            _spawn_quiet(["open", httpd.url])
        elif shutil.which("xdg-open"):
            _spawn_quiet(["xdg-open", httpd.url])

    shutdown_started = threading.Event()

    def _graceful_shutdown():
        try:
            manager.shutdown(cancel=True)
        except Exception as exc:  # shutdown must never mask the exit
            sys.stderr.write("[ui] shutdown while cancelling: %r\n" % exc)
        try:
            httpd.shutdown()
        except Exception:
            pass

    def _signal_handler(signum, frame):
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        thread = threading.Thread(target=_graceful_shutdown, daemon=True)
        thread.start()

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, OSError):
        pass

    exit_code = 0
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_started.set()
        manager.shutdown(cancel=True)
        httpd.server_close()
        stopped = dict(server_json)
        stopped.update({
            "alive": False,
            "pid": None,
            "stopped_at": time.time(),
        })
        _write_server_json(server_json_path, stopped)
        try:
            lock_fh.close()
        except OSError:
            pass
    return exit_code


def _spawn_quiet(argv):
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
