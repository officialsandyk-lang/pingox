from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SSHConfig:
    host: str
    user: str = "ubuntu"
    ssh_key: str | None = None
    port: int = 22
    strict_host_key_checking: bool = False
    connect_timeout_seconds: int = 10


class SSHError(RuntimeError):
    pass


class SSHClient:
    def __init__(self, config: SSHConfig):
        if not config.host:
            raise ValueError("SSH host is required")
        self.config = config

    def _base_ssh_args(self) -> list[str]:
        args = [
            "ssh",
            "-p",
            str(self.config.port),
            "-o",
            f"ConnectTimeout={self.config.connect_timeout_seconds}",
        ]

        if not self.config.strict_host_key_checking:
            args += [
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
            ]

        if self.config.ssh_key:
            args += ["-i", str(Path(self.config.ssh_key).expanduser())]

        args.append(f"{self.config.user}@{self.config.host}")
        return args

    def _base_scp_args(self) -> list[str]:
        args = [
            "scp",
            "-P",
            str(self.config.port),
        ]

        if not self.config.strict_host_key_checking:
            args += [
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
            ]

        if self.config.ssh_key:
            args += ["-i", str(Path(self.config.ssh_key).expanduser())]

        return args

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        check: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        remote_command = command
        if sudo:
            remote_command = f"sudo bash -lc {shlex.quote(command)}"

        cmd = self._base_ssh_args() + [remote_command]

        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

        if check and proc.returncode != 0:
            raise SSHError(
                "Remote command failed\n"
                f"Host: {self.config.user}@{self.config.host}\n"
                f"Command: {command}\n"
                f"Return code: {proc.returncode}\n"
                f"STDOUT:\n{proc.stdout}\n"
                f"STDERR:\n{proc.stderr}"
            )

        return proc

    def upload_file(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        sudo_move: bool = False,
        timeout: int = 120,
    ) -> None:
        local = Path(local_path).expanduser().resolve()
        if not local.exists():
            raise FileNotFoundError(f"Local file not found: {local}")

        if sudo_move:
            tmp_remote = f"/tmp/pingora-ai-upload-{local.name}"
            target = tmp_remote
        else:
            target = remote_path

        cmd = self._base_scp_args() + [
            str(local),
            f"{self.config.user}@{self.config.host}:{target}",
        ]

        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

        if proc.returncode != 0:
            raise SSHError(
                "SCP upload failed\n"
                f"Local: {local}\n"
                f"Remote: {remote_path}\n"
                f"STDOUT:\n{proc.stdout}\n"
                f"STDERR:\n{proc.stderr}"
            )

        if sudo_move:
            self.run(
                f"mkdir -p {shlex.quote(str(Path(remote_path).parent))} "
                f"&& mv {shlex.quote(tmp_remote)} {shlex.quote(remote_path)} "
                f"&& chown root:root {shlex.quote(remote_path)}",
                sudo=True,
            )

    def upload_many(
        self,
        files: Iterable[tuple[str | Path, str]],
        *,
        sudo_move: bool = False,
    ) -> None:
        for local, remote in files:
            self.upload_file(local, remote, sudo_move=sudo_move)
