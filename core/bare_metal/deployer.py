from __future__ import annotations
from core.homepage_route import ensure_route_directory_homepage

import json
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.bare_metal.nginx_writer import render_nginx_conf
from core.bare_metal.ssh_client import SSHClient, SSHConfig
from core.bare_metal.state import (
    BareMetalState,
    color_port,
    default_state,
    opposite_color,
    parse_state,
    serialize_state,
)
from core.bare_metal.systemd_writer import render_pingora_service


REMOTE_ROOT = "/opt/pingora-ai"
REMOTE_RELEASES = f"{REMOTE_ROOT}/releases"
REMOTE_STATE_DIR = f"{REMOTE_ROOT}/state"
REMOTE_BACKUPS = f"{REMOTE_ROOT}/backups"
REMOTE_NGINX_CONF = "/etc/nginx/conf.d/pingora-ai.conf"


@dataclass
class BareMetalDeployOptions:
    host: str
    user: str = "ubuntu"
    ssh_key: str | None = None
    ssh_port: int = 22
    server_name: str = "_"
    public_http_port: int = 80
    binary_path: str = "generated-pingora-proxy/target/release/generated-pingora-proxy"
    service_user: str = "www-data"
    verify_path: str = "/"
    verify_timeout_seconds: int = 60


@dataclass
class BareMetalDeployResult:
    success: bool
    target: str = "bare_metal_linux"
    active_color: str | None = None
    active_port: int | None = None
    live_url: str | None = None
    message: str = ""
    error: str | None = None


class BareMetalLinuxDeployer:
    def _stop_service(self, color: str | None) -> None:
        if color not in {"blue", "green"}:
            return

        service = f"pingora-ai-{color}"

        # Current generated Pingora runtime may not stop cleanly with SIGTERM.
        # Kill first, avoid "disable --now", then clean systemd state.
        self.ssh.run(
            f"systemctl kill -s SIGKILL {service}",
            sudo=True,
            check=False,
            timeout=10,
        )
        self.ssh.run(
            f"pkill -9 -f /opt/pingora-ai/releases/{color}/pingora-gateway",
            sudo=True,
            check=False,
            timeout=10,
        )
        self.ssh.run(
            f"systemctl stop {service}",
            sudo=True,
            check=False,
            timeout=10,
        )
        self.ssh.run(
            f"systemctl disable {service}",
            sudo=True,
            check=False,
            timeout=10,
        )
        self.ssh.run(
            f"systemctl reset-failed {service}",
            sudo=True,
            check=False,
            timeout=10,
        )


    def __init__(self, options: BareMetalDeployOptions):
        self.options = options
        self.ssh = SSHClient(
            SSHConfig(
                host=options.host,
                user=options.user,
                ssh_key=options.ssh_key,
                port=options.ssh_port,
            )
        )

    def deploy(self, config: dict[str, Any]) -> BareMetalDeployResult:
        try:
            self._preflight()

            current_state = self._read_state()
            candidate_color = opposite_color(current_state.active_color)
            candidate_port = color_port(candidate_color)

            release_dir = f"{REMOTE_RELEASES}/{candidate_color}"
            remote_binary = f"{release_dir}/pingora-gateway"
            remote_config = f"{release_dir}/config.json"
            service_name = f"pingora-ai-{candidate_color}"
            service_file = f"/etc/systemd/system/{service_name}.service"

            self._prepare_release_dir(release_dir)
            config = ensure_route_directory_homepage(config)
            self._upload_config(config, remote_config)
            self._upload_static_assets(release_dir)
            self._stop_service(candidate_color)
            self._upload_binary(remote_binary)

            service_text = render_pingora_service(
                service_name=candidate_color,
                binary_path=remote_binary,
                config_path=remote_config,
                working_dir=release_dir,
                listen_port=candidate_port,
                user=self.options.service_user,
            )
            self._upload_text(service_text, service_file, sudo=True)

            self._start_candidate(service_name)
            self._verify_candidate(candidate_port)

            nginx_backup = self._backup_nginx()

            nginx_text = render_nginx_conf(
                active_port=candidate_port,
                server_name=self.options.server_name,
                public_http_port=self.options.public_http_port,
                config=config,
            )
            self._upload_text(nginx_text, REMOTE_NGINX_CONF, sudo=True)

            try:
                self._test_and_reload_nginx()
                self._verify_public()
            except Exception:
                self._restore_nginx(nginx_backup)
                raise

            new_state = BareMetalState(
                active_color=candidate_color,
                active_port=candidate_port,
                previous_color=current_state.active_color,
                previous_port=current_state.active_port,
            )
            self._write_state(new_state)

            self._stop_old_service(current_state.active_color)

            return BareMetalDeployResult(
                success=True,
                active_color=candidate_color,
                active_port=candidate_port,
                live_url=f"http://{self.options.host}:{self.options.public_http_port}/",
                message="Bare-metal Linux blue/green deployment completed successfully.",
            )

        except Exception as exc:
            return BareMetalDeployResult(
                success=False,
                message="Bare-metal Linux deployment failed.",
                error=str(exc),
            )

    def rollback(self) -> BareMetalDeployResult:
        try:
            state = self._read_state()

            if not state.previous_color or not state.previous_port:
                return BareMetalDeployResult(
                    success=False,
                    message="No previous bare-metal deployment state available for rollback.",
                )

            nginx_backup = self._backup_nginx()

            nginx_text = render_nginx_conf(
                active_port=state.previous_port,
                server_name=self.options.server_name,
                public_http_port=self.options.public_http_port,
                config={},
            )
            self._upload_text(nginx_text, REMOTE_NGINX_CONF, sudo=True)

            try:
                self._test_and_reload_nginx()
                self._verify_public()
            except Exception:
                self._restore_nginx(nginx_backup)
                raise

            rolled_state = BareMetalState(
                active_color=state.previous_color,
                active_port=state.previous_port,
                previous_color=state.active_color,
                previous_port=state.active_port,
            )
            self._write_state(rolled_state)

            return BareMetalDeployResult(
                success=True,
                active_color=rolled_state.active_color,
                active_port=rolled_state.active_port,
                live_url=f"http://{self.options.host}:{self.options.public_http_port}/",
                message="Bare-metal Linux rollback completed successfully.",
            )

        except Exception as exc:
            return BareMetalDeployResult(
                success=False,
                message="Bare-metal Linux rollback failed.",
                error=str(exc),
            )

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        services = self.ssh.run(
            "systemctl is-active pingora-ai-blue pingora-ai-green nginx",
            sudo=True,
            check=False,
        )

        return {
            "target": "bare_metal_linux",
            "host": self.options.host,
            "active_color": state.active_color,
            "active_port": state.active_port,
            "previous_color": state.previous_color,
            "previous_port": state.previous_port,
            "services_stdout": services.stdout,
            "services_stderr": services.stderr,
        }

    def _preflight(self) -> None:
        self.ssh.run("echo pingora-ai-ssh-ok")
        self.ssh.run("command -v systemctl")
        self.ssh.run(
            "command -v nginx || (apt-get update && apt-get install -y nginx)",
            sudo=True,
            timeout=300,
        )
        self.ssh.run("command -v curl || (apt-get update && apt-get install -y curl)", sudo=True, timeout=300)
        self.ssh.run("systemctl enable nginx", sudo=True, check=False)
        self.ssh.run("systemctl start nginx", sudo=True, check=False)

        # Disable Ubuntu's default NGINX site so pingora-ai owns port 80.
        self.ssh.run("rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default", sudo=True, check=False)

        self.ssh.run(
            f"mkdir -p {REMOTE_RELEASES} {REMOTE_STATE_DIR} {REMOTE_BACKUPS}",
            sudo=True,
        )
        self.ssh.run(
            f"chown -R {shlex.quote(self.options.user)}:{shlex.quote(self.options.user)} {REMOTE_ROOT}",
            sudo=True,
        )

        self.ssh.run(
            f"id -u {shlex.quote(self.options.service_user)} "
            f"|| useradd --system --no-create-home {shlex.quote(self.options.service_user)}",
            sudo=True,
            check=False,
        )

    def _read_state(self) -> BareMetalState:
        proc = self.ssh.run(
            f"cat {REMOTE_STATE_DIR}/active_state.json",
            check=False,
        )
        if proc.returncode != 0:
            return default_state()

        return parse_state(proc.stdout)

    def _write_state(self, state: BareMetalState) -> None:
        self._upload_text(
            serialize_state(state),
            f"{REMOTE_STATE_DIR}/active_state.json",
            sudo=False,
        )

    def _prepare_release_dir(self, release_dir: str) -> None:
        self.ssh.run(f"mkdir -p {shlex.quote(release_dir)}")
        self.ssh.run(f"chmod 755 {shlex.quote(release_dir)}")


    def _upload_static_assets(self, release_dir: str) -> None:
        """Upload generated static assets for the route-directory homepage."""
        import tarfile
        import tempfile
        import time
        from pathlib import Path

        local_public = Path("generated-pingora-proxy/public")

        if not local_public.exists():
            raise FileNotFoundError(
                "Static homepage assets missing: generated-pingora-proxy/public"
            )

        tmp_tar = Path(tempfile.gettempdir()) / f"pingora-ai-public-{int(time.time())}.tar.gz"
        remote_tar = f"/tmp/{tmp_tar.name}"

        try:
            with tarfile.open(tmp_tar, "w:gz") as tar:
                tar.add(local_public, arcname="public")

            self.ssh.upload_file(tmp_tar, remote_tar)

            self.ssh.run(
                (
                    f"mkdir -p {shlex.quote(release_dir)} && "
                    f"rm -rf {shlex.quote(release_dir)}/public && "
                    f"tar -xzf {shlex.quote(remote_tar)} -C {shlex.quote(release_dir)} && "
                    f"chmod -R 755 {shlex.quote(release_dir)}/public && "
                    f"chown -R {shlex.quote(self.options.user)}:{shlex.quote(self.options.user)} "
                    f"{shlex.quote(release_dir)}/public && "
                    f"rm -f {shlex.quote(remote_tar)}"
                ),
                sudo=True,
                timeout=60,
            )
        finally:
            tmp_tar.unlink(missing_ok=True)


    def _upload_config(self, config: dict[str, Any], remote_config: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            json.dump(config, tmp, indent=2, sort_keys=True)
            tmp_path = Path(tmp.name)

        try:
            self.ssh.upload_file(tmp_path, remote_config)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _upload_binary(self, remote_binary: str) -> None:
        local_binary = Path(self.options.binary_path).expanduser()

        if not local_binary.exists():
            raise FileNotFoundError(
                "Pingora binary not found. Build it first or pass --binary-path.\n"
                f"Expected: {local_binary}"
            )

        # Do not SCP directly over the live/restarting systemd binary.
        # Upload to /tmp first, then move into place after services are stopped.
        tmp_remote = f"/tmp/pingora-ai-upload-{int(time.time())}-pingora-gateway"

        self.ssh.upload_file(local_binary, tmp_remote)

        self.ssh.run(
            f"mkdir -p {shlex.quote(str(Path(remote_binary).parent))} "
            f"&& rm -f {shlex.quote(remote_binary)} "
            f"&& mv {shlex.quote(tmp_remote)} {shlex.quote(remote_binary)} "
            f"&& chmod +x {shlex.quote(remote_binary)} "
            f"&& chown {shlex.quote(self.options.user)}:{shlex.quote(self.options.user)} {shlex.quote(remote_binary)}",
            sudo=True,
        )

    def _upload_text(self, text: str, remote_path: str, *, sudo: bool) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)

        try:
            self.ssh.upload_file(tmp_path, remote_path, sudo_move=sudo)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _start_candidate(self, service_name: str) -> None:
        self.ssh.run("systemctl daemon-reload", sudo=True)
        self.ssh.run(f"systemctl restart {shlex.quote(service_name)}", sudo=True)
        self.ssh.run(f"systemctl enable {shlex.quote(service_name)}", sudo=True, check=False)

    def _verify_candidate(self, port: int) -> None:
        deadline = time.time() + self.options.verify_timeout_seconds
        last_output = ""

        while time.time() < deadline:
            proc = self.ssh.run(
                f"curl -fsS http://127.0.0.1:{port}{self.options.verify_path}",
                check=False,
                timeout=10,
            )

            if proc.returncode == 0:
                return

            last_output = proc.stdout + "\n" + proc.stderr
            time.sleep(2)

        raise RuntimeError(
            f"Candidate failed health check on 127.0.0.1:{port}{self.options.verify_path}\n"
            f"{last_output}"
        )

    def _verify_public(self) -> None:
        deadline = time.time() + self.options.verify_timeout_seconds
        last_output = ""

        while time.time() < deadline:
            proc = self.ssh.run(
                f"curl -fsS http://127.0.0.1:{self.options.public_http_port}{self.options.verify_path}",
                check=False,
                timeout=10,
            )

            if proc.returncode == 0:
                return

            last_output = proc.stdout + "\n" + proc.stderr
            time.sleep(2)

        raise RuntimeError(
            f"Public NGINX route failed health check on 127.0.0.1:{self.options.public_http_port}{self.options.verify_path}\n"
            f"{last_output}"
        )

    def _backup_nginx(self) -> str:
        stamp = str(int(time.time()))
        backup_path = f"{REMOTE_BACKUPS}/nginx-pingora-ai-{stamp}.conf"

        self.ssh.run(
            f"if [ -f {REMOTE_NGINX_CONF} ]; then "
            f"cp {REMOTE_NGINX_CONF} {backup_path}; "
            f"else touch {backup_path}; fi",
            sudo=True,
        )

        return backup_path

    def _restore_nginx(self, backup_path: str) -> None:
        self.ssh.run(
            f"cp {shlex.quote(backup_path)} {REMOTE_NGINX_CONF} "
            f"&& nginx -t "
            f"&& systemctl reload nginx",
            sudo=True,
            check=False,
        )

    def _test_and_reload_nginx(self) -> None:
        self.ssh.run("nginx -t", sudo=True)
        self.ssh.run("systemctl reload nginx", sudo=True)

    def _stop_old_service(self, old_color: str | None) -> None:
        if old_color not in {"blue", "green"}:
            return

        self.ssh.run(
            f"systemctl stop pingora-ai-{old_color}",
            sudo=True,
            check=False,
        )
