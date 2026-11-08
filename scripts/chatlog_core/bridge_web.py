"""Loopback-only management; bearer secret is provisioned locally, never logged."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .common import QueryError

ASSETS = Path(__file__).parent / 'bridge_ui'


def handler(bridge, token, port):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'ChatlogBridge'

        def log_message(self, *_):
            pass

        def respond(self, code, value, content_type='application/json; charset=utf-8'):
            body = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            origins = {'http://' + h for h in hosts}
            return (self.headers.get('Host') in hosts and
                    (not self.headers.get('Origin') or self.headers['Origin'] in origins))

        def authenticated(self):
            return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)

        def do_GET(self):
            if not self.allowed():
                return self.respond(403, dict(error='local_origin_required'))
            url = urlsplit(self.path)
            if url.path in ('/', '/bridge_ui.js', '/bridge_ui.css'):
                name, mime = {'/': ('index.html', 'text/html; charset=utf-8'), '/bridge_ui.js': ('bridge_ui.js', 'text/javascript; charset=utf-8'), '/bridge_ui.css': ('bridge_ui.css', 'text/css; charset=utf-8')}[url.path]
                return self.respond(200, (ASSETS / name).read_bytes(), mime)
            if not self.authenticated():
                return self.respond(401, dict(error='authorization_required'))
            try:
                if url.path == '/api/state':
                    return self.respond(200, bridge.status())
                if url.path == '/api/catalog':
                    backend=parse_qs(url.query).get('backend',['codex'])[0]
                    return self.respond(200, dict(**bridge.runner(backend).catalog(), accounts=[dict(platform=p, account_key=b['account_key'], self_id=b['self_id']) for p, b in bridge.accounts().items()]))
                if url.path == '/api/contacts':
                    query = parse_qs(url.query).get('q', [''])[0].strip()
                    if not 1 <= len(query) <= 100:
                        raise QueryError('invalid_search', '输入 1–100 字的姓名或精确 ID')
                    return self.respond(200, bridge.feed.contacts(query))
                self.respond(404, dict(error='not_found'))
            except QueryError as e:
                self.respond(400, e.detail)
            except Exception:
                self.respond(503, dict(error='service_unavailable'))

        def do_POST(self):
            reply=self.path=='/api/reply'
            if not self.allowed() or (not reply and not self.authenticated()):
                return self.respond(403, dict(error='authorization_required'))
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 131072 or self.headers.get('Content-Type') != 'application/json':
                    return self.respond(400, dict(error='invalid_json_request'))
                value = json.loads(self.rfile.read(length))
                if reply:
                    # This capability authorizes one exact reply, not management.
                    capability=self.headers.get('Authorization','').removeprefix('Bearer ')
                    if not re.fullmatch(r'[A-Za-z0-9_-]{32,100}',capability) or not isinstance(value.get('session_id'),str) or not value['session_id']:
                        return self.respond(403,dict(error='reply_authorization_required'))
                    options=dict(caller_session_id=value['session_id'])
                    if value.get('attachments') is not None:options['attachments']=value['attachments']
                    result=bridge.reply(capability,value.get('text'),**options)
                    return self.respond(200,result)
                if self.path == '/api/config':
                    return self.respond(200, bridge.save(value))
                if self.path == '/api/pause':
                    config = bridge.store.config()
                    config['paused'] = value['paused']
                    return self.respond(200, bridge.save(config))
                self.respond(404, dict(error='not_found'))
            except QueryError as e:
                self.respond(400, e.detail)
            except (ValueError, KeyError, TypeError):
                self.respond(400, dict(error='invalid_request'))
            except Exception:
                self.respond(503, dict(error='save_failed'))
    return Handler


def server(bridge, port=27146):
    httpd = ThreadingHTTPServer(('127.0.0.1', port), handler(bridge, bridge.store.admin_token(), port))
    httpd.RequestHandlerClass = handler(bridge, bridge.store.admin_token(), httpd.server_port)
    httpd.daemon_threads = True
    return httpd
