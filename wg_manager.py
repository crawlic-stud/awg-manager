from __future__ import annotations

import os
import argparse
import base64
import json
import re
import shlex
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import paramiko
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

DEFAULT_CONTAINERS = ("amnezia-awg", "amnezia-awg-legacy", "amnezia-awg2", "amnezia-awg3")
SERVER_NAME = os.getenv("SERVER_NAME", "AWG Server")
AWG_CONFIG_KEYS = (
    "Jc",
    "Jmin",
    "Jmax",
    "S1",
    "S2",
    "S3",
    "S4",
    "H1",
    "H2",
    "H3",
    "H4",
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    "HeaderProtectionKey",
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
    "RandomTrailers",
    "DisableCookies",
)


def generate_wg_keypair() -> tuple[str, str]:
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        base64.b64encode(private_bytes).decode("utf-8"),
        base64.b64encode(public_bytes).decode("utf-8"),
    )


def encode_vpn_link(config_text: str) -> str:
    return f"vpn://{base64.urlsafe_b64encode(config_text.strip().encode('utf-8')).decode('utf-8').rstrip('=')}"


def parse_wg_config(config_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in str(config_text or "").split("\n"):
        line = line.strip()
        if not line or (line.startswith("[") and line.endswith("]")):
            continue
        sep = line.find("=")
        if sep > 0:
            values[line[:sep].strip()] = line[sep + 1 :].strip()
    return values


def build_amnezia_config(config_text: str, description: str) -> dict | None:
    if not description:
        return None
    values = parse_wg_config(config_text)
    host, _, port = values.get("Endpoint", "").rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit():
        return None
    if not (values.get("PrivateKey") and values.get("Address") and values.get("PublicKey")):
        return None

    last_config: dict[str, object] = {
        "config": str(config_text),
        "hostName": host,
        "port": int(port),
        "client_priv_key": values["PrivateKey"],
        "client_ip": values["Address"],
        "server_pub_key": values["PublicKey"],
    }
    psk = values.get("PresharedKey") or values.get("PreSharedKey")
    if psk:
        last_config["psk_key"] = psk
    if values.get("PersistentKeepalive"):
        last_config["persistent_keep_alive"] = values["PersistentKeepalive"]
    last_config["allowed_ips"] = [part.strip() for part in values.get("AllowedIPs", "").split(",") if part.strip()]

    protocol_name = "wireguard"
    for key in AWG_CONFIG_KEYS:
        if values.get(key):
            last_config[key] = values[key]
            protocol_name = "awg"

    last_config["mtu"] = values.get("MTU") or ("1376" if protocol_name == "awg" else "1420")
    container = "amnezia-awg" if protocol_name == "awg" else "amnezia-wireguard"
    config = {
        "containers": [
            {
                "container": container,
                protocol_name: {
                    "last_config": json.dumps(last_config, indent=4) + "\n",
                    "isThirdPartyConfig": True,
                    "port": str(port),
                    "transport_proto": "udp",
                },
            }
        ],
        "defaultContainer": container,
        "description": description,
        "hostName": host,
    }
    dns = [part.strip() for part in values.get("DNS", "").split(",") if part.strip()]
    if len(dns) >= 2:
        config["dns1"], config["dns2"] = dns[0], dns[1]
    return config


def amnezia_config_bytes(config_text: str, description: str) -> bytes:
    config = build_amnezia_config(config_text, description)
    if not config:
        return b""
    raw = json.dumps(config, indent=4).encode("utf-8")
    return struct.pack(">I", len(raw)) + zlib.compress(raw, 8)


def amnezia_vpn_key(config_text: str, description: str) -> str:
    payload = amnezia_config_bytes(config_text, description)
    if not payload:
        return ""
    return base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")


@dataclass
class Client:
    client_id: str
    name: str
    ip: str = ""
    enabled: bool = True
    private_key: str = ""
    psk: str = ""
    vpn_link: str = ""
    raw: dict | None = None


class SSHSession:
    def __init__(self, host: str, username: str, port: int = 22, password: str | None = None, key: str | None = None):
        self.host = host
        self.username = username
        self.port = port
        self.password = password
        self.key = key
        self.client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "allow_agent": False,
            "look_for_keys": False,
            "timeout": 7,
            "banner_timeout": 7,
            "auth_timeout": 7,
        }
        if self.key:
            key_file = Path(self.key).expanduser()
            key_text = key_file.read_text()
            from io import StringIO

            key_obj = None
            for loader in (
                paramiko.RSAKey.from_private_key,
                paramiko.Ed25519Key.from_private_key,
                paramiko.ECDSAKey.from_private_key,
            ):
                try:
                    key_obj = loader(StringIO(key_text))
                    break
                except Exception:
                    continue
            if key_obj is None:
                raise ValueError(f"Unsupported private key: {self.key}")
            kwargs["pkey"] = key_obj
        elif self.password:
            kwargs["password"] = self.password
        client.connect(**kwargs)
        self.client = client

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def run(
        self, command: str, sudo: bool = False, stdin: str | None = None, timeout: int = 60
    ) -> tuple[str, str, int]:
        if not self.client:
            self.connect()
        assert self.client is not None
        if sudo and self.username != "root":
            if self.password:
                command = f"echo {shlex.quote(self.password)} | sudo -S -p '' {command}"
            else:
                command = f"sudo {command}"
        stdin_f, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        if stdin is not None:
            stdin_f.write(stdin)
            stdin_f.flush()
            stdin_f.channel.shutdown_write()
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return out.strip(), err.strip(), exit_code


class WGManager:
    def __init__(
        self,
        host: str,
        username: str,
        password: str | None = None,
        key: str | None = None,
        port: int = 22,
        endpoint_host: str | None = None,
        container: str | None = None,
    ):
        self.host = host
        self.endpoint_host = endpoint_host or host
        self.ssh = SSHSession(host, username, port=port, password=password, key=key)
        self.container = container
        self.config_path: str | None = None

    def connect(self) -> bool:
        self.ssh.connect()
        self.container = self.container or self._detect_container()
        if not self.container:
            raise RuntimeError("amnezia-wg container not found")
        self.config_path = self._detect_config_path()
        if not self.config_path:
            raise RuntimeError("amnezia-wg config not found")
        return True

    def _run(self, command: str, sudo: bool = True, stdin: str | None = None) -> str:
        out, err, code = self.ssh.run(command, sudo=sudo, stdin=stdin)
        if code != 0:
            raise RuntimeError(err or out or f"command failed: {command}")
        return out

    def _detect_container(self) -> str | None:
        out = self._run("docker ps -a --format '{{.Names}}'", sudo=True)
        names = {line.strip() for line in out.splitlines() if line.strip()}
        for candidate in DEFAULT_CONTAINERS:
            for name in sorted(names):
                if name == candidate or name.startswith(candidate + "-"):
                    return name
        return None

    def _detect_config_path(self) -> str:
        assert self.container
        script = 'for p in /opt/amnezia/awg/awg0.conf /opt/amnezia/awg/wg0.conf; do [ -f "$p" ] && echo "$p" && exit 0; done; exit 1'
        return self._run(
            f"docker exec -i {shlex.quote(self.container)} sh -c {shlex.quote(script)}", sudo=True
        ).splitlines()[0]

    def is_installed(self) -> bool:
        try:
            self.connect()
            return True
        except Exception:
            return False

    def _read_file(self, path: str) -> str:
        assert self.container
        return self._run(f"docker exec -i {shlex.quote(self.container)} cat {shlex.quote(path)}", sudo=True)

    def _write_file(self, path: str, content: str) -> None:
        assert self.container
        self._run(
            f"docker exec -i {shlex.quote(self.container)} sh -c {shlex.quote(f'cat > {path}')}",
            sudo=True,
            stdin=content,
        )

    def _server_config(self) -> str:
        assert self.config_path
        return self._read_file(self.config_path)

    def _server_public_key(self) -> str:
        assert self.container
        return self._read_file("/opt/amnezia/awg/wireguard_server_public_key.key").strip()

    def _server_psk(self) -> str:
        assert self.container
        return self._read_file("/opt/amnezia/awg/wireguard_psk.key").strip()

    def _listen_port(self) -> str:
        config = self._server_config()
        match = re.search(r"(?m)^ListenPort\s*=\s*(\d+)\s*$", config)
        return match.group(1) if match else "55424"

    def _awg_extra_lines(self) -> list[tuple[str, str]]:
        values = parse_wg_config(self._server_config())
        extra = []
        for key in AWG_CONFIG_KEYS:
            value = values.get(key)
            if value:
                extra.append((key, value))
        return extra

    def _clients_table_raw(self) -> list[dict]:
        assert self.container
        try:
            text = self._read_file("/opt/amnezia/awg/clientsTable")
        except Exception:
            return []
        try:
            data = json.loads(text)
        except Exception:
            return []
        if isinstance(data, list):
            return data
        return []

    def _client_rows(self) -> dict[str, dict]:
        rows = {}
        for item in self._clients_table_raw():
            client_id = item.get("clientId", "")
            if client_id:
                rows[client_id] = item
        return rows

    def _peer_ips_from_config(self) -> dict[str, str]:
        config = self._server_config()
        peers = {}
        for block in re.split(r"\n(?=\[Peer\])", config):
            if not block.lstrip().startswith("[Peer]"):
                continue
            pub = re.search(r"(?m)^PublicKey\s*=\s*(.+)\s*$", block)
            ips = re.search(r"(?m)^AllowedIPs\s*=\s*([0-9.]+)", block)
            if pub and ips:
                peers[pub.group(1).strip()] = ips.group(1).strip()
        return peers

    def _subnet_base(self) -> str:
        config = self._server_config()
        match = re.search(r"(?m)^Address\s*=\s*([0-9.]+)\.(\d+)/(\d+)\s*$", config)
        if match:
            return ".".join(match.group(1).split(".")[:3] + ["0"])
        match = re.search(r"(?m)^Address\s*=\s*([0-9.]+/\d+)", config)
        if match:
            return match.group(1).split("/")[0].rsplit(".", 1)[0] + ".0"
        return "10.8.1.0"

    def _used_ips(self) -> set[str]:
        used = set(self._peer_ips_from_config().values())
        for item in self._clients_table_raw():
            ip = (item.get("userData") or {}).get("clientIp")
            if ip:
                used.add(f"{ip}/32")
        return used

    def _next_ip(self) -> str:
        base = self._subnet_base().split(".")
        prefix = ".".join(base[:3])
        used_octets = set()
        for entry in self._used_ips():
            ip = entry.split("/", 1)[0]
            parts = ip.split(".")
            if len(parts) == 4 and ".".join(parts[:3]) == prefix:
                try:
                    used_octets.add(int(parts[3]))
                except ValueError:
                    pass
        for octet in range(2, 255):
            if octet not in used_octets:
                return f"{prefix}.{octet}"
        raise RuntimeError("No free client IPs left")

    def list_clients(self) -> list[Client]:
        rows = self._client_rows()
        peers = self._peer_ips_from_config()
        result: list[Client] = []
        seen: set[str] = set()
        for client_id, ip in peers.items():
            row = rows.get(client_id, {})
            ud = row.get("userData") or {}
            result.append(
                Client(
                    client_id=client_id,
                    name=ud.get("clientName", client_id[:10]),
                    ip=ip.split("/", 1)[0],
                    enabled=bool(ud.get("enabled", True)),
                    private_key=ud.get("clientPrivateKey", ""),
                    psk=ud.get("psk", ""),
                    raw=row,
                )
            )
            seen.add(client_id)
        for client_id, row in rows.items():
            if client_id in seen:
                continue
            ud = row.get("userData") or {}
            result.append(
                Client(
                    client_id=client_id,
                    name=ud.get("clientName", client_id[:10]),
                    ip=ud.get("clientIp", ""),
                    enabled=bool(ud.get("enabled", True)),
                    private_key=ud.get("clientPrivateKey", ""),
                    psk=ud.get("psk", ""),
                    raw=row,
                )
            )
        return result

    def _build_client_config(self, name: str, client_ip: str, private_key: str, psk: str) -> str:
        server_pub = self._server_public_key()
        port = self._listen_port()
        lines = [
            "[Interface]",
            f"Address = {client_ip}/32",
            "DNS = 1.1.1.1, 1.0.0.1",
            f"PrivateKey = {private_key}",
            "MTU = 1376",
        ]
        lines.extend(f"{key} = {value}" for key, value in self._awg_extra_lines())
        lines.extend(
            [
                "",
                "[Peer]",
                f"PublicKey = {server_pub}",
                f"PresharedKey = {psk}",
                "AllowedIPs = 0.0.0.0/0",
                f"Endpoint = {self.endpoint_host}:{port}",
                "PersistentKeepalive = 25",
                "",
            ]
        )
        return "\n".join(lines)

    def add_client(self, name: str) -> Client:
        if not self.container or not self.config_path:
            self.connect()
        assert self.container and self.config_path
        private_key, public_key = generate_wg_keypair()
        client_ip = self._next_ip()
        psk = self._server_psk()
        config = self._build_client_config(name, client_ip, private_key, psk)
        config_text = self._server_config().rstrip() + (
            "\n\n[Peer]\n" f"PublicKey = {public_key}\n" f"PresharedKey = {psk}\n" f"AllowedIPs = {client_ip}/32\n"
        )
        self._write_file(self.config_path, config_text + "\n")
        self._run(
            f"docker exec -i {shlex.quote(self.container)} bash -lc "
            + shlex.quote(f"awg syncconf awg0 <(awg-quick strip {self.config_path})"),
            sudo=True,
        )
        table = self._clients_table_raw()
        table.append(
            {
                "clientId": public_key,
                "userData": {
                    "clientName": name,
                    "creationDate": datetime.now().isoformat(),
                    "clientPrivateKey": private_key,
                    "clientIp": client_ip,
                    "psk": psk,
                    "enabled": True,
                },
            }
        )
        self._write_file("/opt/amnezia/awg/clientsTable", json.dumps(table, indent=2))
        return Client(
            client_id=public_key,
            name=name,
            ip=client_ip,
            enabled=True,
            private_key=private_key,
            psk=psk,
            vpn_link=self.get_vpn_link_from_config(config, SERVER_NAME),
            raw={"config": config, "vpn_link": self.get_vpn_link_from_config(config, SERVER_NAME)},
        )

    def delete_client(self, client_id_or_name: str) -> bool:
        if not self.container or not self.config_path:
            self.connect()
        assert self.container and self.config_path

        client = self.get_client(client_id_or_name)
        config = self._server_config()
        blocks = []
        removed = False
        for block in re.split(r"\n(?=\[Peer\])", config):
            if not block.strip():
                continue
            if block.lstrip().startswith("[Peer]"):
                pub = re.search(r"(?m)^PublicKey\s*=\s*(.+)\s*$", block)
                if pub and pub.group(1).strip() == client.client_id:
                    removed = True
                    continue
            blocks.append(block.rstrip())

        if removed:
            new_config = "\n\n".join(blocks).rstrip() + "\n"
            self._write_file(self.config_path, new_config)
            self._run(
                f"docker exec -i {shlex.quote(self.container)} bash -lc "
                + shlex.quote(f"awg syncconf awg0 <(awg-quick strip {self.config_path})"),
                sudo=True,
            )

        table = self._clients_table_raw()
        new_table = [item for item in table if item.get("clientId") != client.client_id]
        if len(new_table) != len(table):
            self._write_file("/opt/amnezia/awg/clientsTable", json.dumps(new_table, indent=2))

        return removed or len(new_table) != len(table)

    def get_client(self, client_id_or_name: str) -> Client:
        for client in self.list_clients():
            if client.client_id == client_id_or_name or client.name == client_id_or_name:
                return client
        raise KeyError(f"client not found: {client_id_or_name}")

    def get_client_config(self, client_id_or_name: str) -> str:
        client = self.get_client(client_id_or_name)
        if client.private_key and client.ip:
            return self._build_client_config(
                client.name, client.ip, client.private_key, client.psk or self._server_psk()
            )
        if client.raw and client.raw.get("config"):
            return client.raw["config"]
        raise RuntimeError("client config cannot be reconstructed")

    def get_vpn_link(self, client_id_or_name: str) -> str:
        client = self.get_client(client_id_or_name)
        return self.get_vpn_link_from_config(self.get_client_config(client_id_or_name), SERVER_NAME)

    def get_vpn_link_from_config(self, config_text: str, description: str) -> str:
        key = amnezia_vpn_key(config_text, description)
        if key:
            return f"vpn://{key}"
        return encode_vpn_link(config_text)


def _print_clients(clients: list[Client]) -> None:
    for client in clients:
        print(f"{client.client_id}\t{client.name}\t{client.ip}\t{'on' if client.enabled else 'off'}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="awg-manager")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password")
    parser.add_argument("--key")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--endpoint-host")
    parser.add_argument("--container")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("list")
    add_p = sub.add_parser("add")
    add_p.add_argument("name")
    get_p = sub.add_parser("vpn")
    get_p.add_argument("client")
    args = parser.parse_args(argv)

    mgr = WGManager(
        host=args.host,
        username=args.user,
        password=args.password,
        key=args.key,
        port=args.port,
        endpoint_host=args.endpoint_host,
        container=args.container,
    )
    try:
        if args.cmd == "check":
            mgr.connect()
            print(f"ok\tcontainer={mgr.container}\tconfig={mgr.config_path}")
        elif args.cmd == "list":
            mgr.connect()
            _print_clients(mgr.list_clients())
        elif args.cmd == "add":
            client = mgr.add_client(args.name)
            assert client.raw is not None, "client.raw is None"
            print(
                json.dumps(
                    {
                        "client_id": client.client_id,
                        "name": client.name,
                        "ip": client.ip,
                        "config": client.raw["config"],
                        "vpn_link": client.raw["vpn_link"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.cmd == "vpn":
            mgr.connect()
            print(mgr.get_vpn_link(args.client))
        return 0
    finally:
        mgr.ssh.close()


if __name__ == "__main__":
    raise SystemExit(main())
