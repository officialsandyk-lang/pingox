"""Real binary tests: live reload, rollback, in-flight safety, TLS and bounded load."""
from __future__ import annotations
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.production import ProductionConfig, generate
from core.deployment_targets import DeploymentOptions
from core.config_store import publish, atomic_write

held, release = threading.Event(), threading.Event()


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/hold':
            held.set()
            release.wait(15)
        data = json.dumps({'port': self.server.server_port, 'host': self.headers.get('host')}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix='pingox-enterprise-') as temporary:
        root = Path(temporary)
        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.pem', '-days', '2', '-subj', '/CN=PingoxTestCA', '-addext', 'basicConstraints=critical,CA:TRUE')
        openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'server.key', '-out', 'server.csr', '-subj', '/CN=localhost')
        (root / 'extensions').write_text('subjectAltName=DNS:localhost\nbasicConstraints=critical,CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n')
        openssl('x509', '-req', '-in', 'server.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial', '-out', 'server.pem', '-days', '2', '-extfile', 'extensions')
        openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'client.key', '-out', 'client.csr', '-subj', '/CN=pingox-client')
        (root / 'client-extensions').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth\n')
        openssl('x509', '-req', '-in', 'client.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial', '-out', 'client.pem', '-days', '2', '-extfile', 'client-extensions')
        servers = [ThreadingHTTPServer(('127.0.0.1', 0), Backend) for _ in range(4)]
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(root / 'server.pem', root / 'server.key')
        servers[2].socket = tls.wrap_socket(servers[2].socket, server_side=True)
        mutual = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        mutual.minimum_version = ssl.TLSVersion.TLSv1_2
        mutual.load_cert_chain(root / 'server.pem', root / 'server.key')
        mutual.load_verify_locations(root / 'ca.pem')
        mutual.verify_mode = ssl.CERT_REQUIRED
        servers[3].socket = mutual.wrap_socket(servers[3].socket, server_side=True)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        ports = [s.server_port for s in servers]
        port = free_port()
        config = ProductionConfig.model_validate({'port': port, 'routes': [
            {'path': '/', 'upstreams': [{'address': f'127.0.0.1:{ports[0]}'}]},
            {'path': '/tls', 'upstreams': [{'address': f'127.0.0.1:{ports[2]}', 'tls': True, 'server_name': 'localhost'}]},
            {'path': '/mtls', 'upstreams': [{'address': f'127.0.0.1:{ports[3]}', 'tls': True, 'server_name': 'localhost'}]},
            {'path': '/bad-name', 'upstreams': [{'address': f'127.0.0.1:{ports[2]}', 'tls': True, 'server_name': 'wrong.invalid'}]},
        ], 'security': {'rate_limit_per_minute': 0, 'upstream_timeout_seconds': 10, 'max_connections': 64}})
        project = generate(config, root / 'build', DeploymentOptions(target='systemd'))
        target = Path(os.environ.get('CARGO_TARGET_DIR', 'tmp/cargo-target')).resolve()
        subprocess.run(['cargo', 'build', '--locked', '--manifest-path', str(project / 'Cargo.toml'), '--target-dir', str(target)], check=True)
        binary = target / 'debug/generated-pingora-proxy'
        state = root / 'state'
        publish(state, config, 0)
        env = dict(os.environ, GATEWAY_BIND_HOST='127.0.0.1', PINGOX_CONFIG_FILE=str(state / 'active.json'), PINGOX_CA_FILE=str(root / 'ca.pem'), PINGOX_API_KEYS_FILE=str(root / 'keys.json'), PINGOX_CLIENT_CERT_FILE=str(root / 'client.pem'), PINGOX_CLIENT_KEY_FILE=str(root / 'client.key'))
        env.pop('TRUSTED_PROXY_CIDRS', None)
        logfile = (root / 'gateway.log').open('w')
        process = subprocess.Popen([str(binary)], env=env, stdout=logfile, stderr=logfile)
        def request(path, key=None):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
            try:
                conn.request('GET', path, headers={'X-Pingox-Api-Key': key} if key else {})
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()
        def wait_revision(revision):
            for _ in range(100):
                try:
                    if json.loads(request('/_gateway/config')[1])['revision'] == revision:
                        return
                except (OSError, ValueError): pass
                if process.poll() is not None:
                    raise AssertionError((root / 'gateway.log').read_text())
                time.sleep(.1)
            raise AssertionError(f'revision {revision} did not become active')
        try:
            wait_revision(1)
            assert request('/tls')[0] == 200
            assert request('/mtls')[0] == 200
            assert json.loads(request('/tls')[1])['host'] == 'localhost'
            assert request('/bad-name')[0] == 502, 'TLS hostname mismatch accepted'
            with concurrent.futures.ThreadPoolExecutor() as executor:
                pending = executor.submit(request, '/hold')
                assert held.wait(2)
                candidate = config.model_copy(deep=True)
                candidate.routes[0].upstreams[0].address = f'127.0.0.1:{ports[1]}'
                publish(state, candidate, 1)
                wait_revision(2)
                assert json.loads(request('/new')[1])['port'] == ports[1]
                release.set()
                assert json.loads(pending.result()[1])['port'] == ports[0], 'in-flight request changed snapshot'
            good = (state / 'active.json').read_bytes()
            atomic_write(state / 'active.json', b'{"revision":3,"routes":[]}')
            time.sleep(1.3)
            assert json.loads(request('/_gateway/config')[1])['revision'] == 2
            assert json.loads(request('/new')[1])['port'] == ports[1]
            assert json.loads(request('/_gateway/config')[1])['rejected_updates'] >= 1
            atomic_write(state / 'active.json', good)
            publish(state, None, 2)
            wait_revision(3)
            assert json.loads(request('/new')[1])['port'] == ports[0]
            # Schema parity fixtures: both Python and native validation must reject them.
            bads = [dict(config.model_dump(), unexpected=True), dict(config.model_dump(), port=80),
                    dict(config.model_dump(), revision=0)]
            for value in bads:
                path = root / 'invalid.json'
                path.write_text(json.dumps(value))
                assert subprocess.run([str(binary), '--check-config', str(path)], capture_output=True).returncode == 1
            # Functional concurrent-load check; this is not a throughput certification.
            def batch(_):
                return [request('/load')[0] for _ in range(50)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(batch, range(8)))
            assert all(status == 200 for result in results for status in result)
            time.sleep(.1)
            assert b'pingox_active_requests 0' in request('/_gateway/metrics')[1]
            import secrets
            old_key, new_key = secrets.token_hex(32), secrets.token_hex(32)
            atomic_write(root / 'keys.json', json.dumps([old_key]).encode(), 0o600)
            protected = config.model_copy(deep=True)
            protected.security.require_api_key = True
            publish(state, protected, 3)
            wait_revision(4)
            assert request('/new')[0] == 401
            assert request('/new', old_key)[0] == 200
            atomic_write(root / 'keys.json', json.dumps([new_key]).encode(), 0o600)
            publish(state, protected, 4)
            wait_revision(5)
            assert request('/new', old_key)[0] == 401
            assert request('/new', new_key)[0] == 200
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=40)
            assert process.returncode == 0
            # The same certificate must fail when its CA is not trusted.
            env.pop('PINGOX_CLIENT_CERT_FILE')
            env.pop('PINGOX_CLIENT_KEY_FILE')
            process = subprocess.Popen([str(binary)], env=env, stdout=logfile, stderr=logfile)
            wait_revision(5)
            assert request('/mtls', new_key)[0] == 502, 'mTLS accepted without a client identity'
            assert request('/tls', new_key)[0] == 200
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=40)
            env.pop('PINGOX_CA_FILE')
            process = subprocess.Popen([str(binary)], env=env, stdout=logfile, stderr=logfile)
            wait_revision(5)
            assert request('/tls', new_key)[0] == 502, 'untrusted TLS certificate accepted'
            from core.local_service import digest
            from core.release_bundle import package, install, rollback
            import shutil
            shutil.copy2(binary, project / 'gateway')
            (project / 'native-release.json').write_text(json.dumps({
                'binary_sha256': digest(project / 'gateway'),
                'config_sha256': digest(project / 'production.json')}))
            openssl('genpkey', '-algorithm', 'ED25519', '-out', 'signing.key')
            openssl('pkey', '-in', 'signing.key', '-pubout', '-out', 'signing.pub')
            installed = root / 'installed'
            package(project, root / 'one.tar.gz', root / 'signing.key', '0.2.0')
            assert install(root / 'one.tar.gz', installed, root / 'signing.pub') == '0.2.0'
            first = (installed / 'current').resolve()
            package(project, root / 'two.tar.gz', root / 'signing.key', '0.2.1')
            assert install(root / 'two.tar.gz', installed, root / 'signing.pub') == '0.2.1'
            assert (installed / 'current').resolve() != first
            assert rollback(installed, root / 'signing.pub') == '0.2.0'
            assert (installed / 'current').resolve() == first
            print('PASS: live reload, in-flight snapshots, invalid-update retention, rollback, TLS trust/hostname/mTLS checks, 400 concurrent requests, live authentication policy/key rotation, signed native install/upgrade/rollback')
        finally:
            release.set()
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            logfile.close()
            for server in servers:
                server.shutdown(); server.server_close()


if __name__ == '__main__': main()
