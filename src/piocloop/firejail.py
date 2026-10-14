"""Optional Firejail sandboxing for the `pi` subprocess.

piocloop starts a fresh `pi --mode rpc` child on every loop iteration, so a
sandbox has to be created once and reused for the whole run.  A
:class:`FirejailSession` owns a temp directory (mode 755) that lives as long as
the run: the netfilter rules and hosts files live there, and ``piocloop`` passes
the same ``argv`` prefix to every spawned child.

The hard part of this module is turning a small JSONC config into a Firejail
command line plus, when you opt into network restriction, an iptables-restore
rules file and a static hosts file so no DNS lookup is needed inside the sandbox.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Hardening that applies to every firejailed run, whether or not networking is
# restricted.
_HARDENING = ["firejail", "--noprofile", "--quiet", "--caps.drop=all", "--nonewprivs"]

_ALLOWED_CONFIG_KEYS = frozenset({"allow", "whitelist", "read_only", "blacklist", "iface"})

# Hostnames: letters, digits, hyphens and dots only.
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


def strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments, but never inside a JSON string.

    A URL like ``"http://x"`` must survive verbatim, so the parser tracks
    whether it is inside a quoted string (and honours backslash escapes).
    Comment removal is replaced with a newline so line/column positions stay
    roughly aligned for error reporting.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    escaped = False

    while i < n:
        ch = text[i]

        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            escaped = False
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i + 2)
            i = n if j == -1 else j
            out.append("\n")
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append("\n")
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _validate_host(host: object) -> str:
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise ValueError(f"invalid hostname in firejail config: {host!r}")
    return host


def _validate_port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"invalid port in firejail config: {port!r} (must be 1-65535)")
    return port


def _validate_string_list(name: str, value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"firejail config key {name!r} must be a list of strings")


def load_config(path: str) -> dict:
    """Load and validate a JSONC firejail config file.

    Strips comments, parses JSON and rejects unknown keys / malformed `allow`
    entries.  Raising ``ValueError`` with a clear message on any problem.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(strip_jsonc(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in firejail config {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"firejail config {path} must be a JSON object")

    unknown = set(data) - _ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"firejail config {path} has unknown key(s): {', '.join(sorted(unknown))}"
        )

    if "iface" in data:
        if not isinstance(data["iface"], str):
            raise ValueError("firejail config key 'iface' must be a string")

    for key in ("whitelist", "read_only", "blacklist"):
        if key in data:
            _validate_string_list(key, data[key])

    allow = data.get("allow") or []
    if not isinstance(allow, list):
        raise ValueError("firejail config key 'allow' must be a list")
    for entry in allow:
        if not isinstance(entry, dict) or "host" not in entry or "port" not in entry:
            raise ValueError(f"firejail 'allow' entry must have 'host' and 'port': {entry!r}")
        _validate_host(entry["host"])
        _validate_port(entry["port"])

    return data


def _write(path: str, content: str) -> None:
    """Write a file with mode 644."""
    Path(path).write_text(content, encoding="utf-8")
    os.chmod(path, 0o644)


def _default_iface() -> str:
    """Best-effort network interface from `ip route show default`.

    Returns "" if it cannot be determined (no `ip`, no default route).
    """
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    line = (out.stdout or "").strip()
    if "dev" in line:
        return line.split("dev", 1)[1].strip().split()[0]
    return ""


def _resolve_ipv4(host: str) -> str:
    """Resolve a host to a single IPv4 address, or raise a clear error."""
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve host {host!r} in firejail config: {exc}") from exc
    if not infos:
        raise ValueError(f"host {host!r} in firejail config resolved to no IPv4 address")
    return infos[0][4][0]


class FirejailSession:
    """Owns the firejail ``prefix`` plus a temp directory for the whole run.

    The temp directory holds the netfilter rules and hosts files and is removed
    on :meth:`close`.  Usable as a context manager; also idempotent if called
    more than once (cli.py calls ``close()`` in a ``finally`` and via ``atexit``
    as a backup).
    """

    def __init__(self, prefix: list[str]) -> None:
        self.prefix = list(prefix)
        self._tmpdir = tempfile.mkdtemp()
        os.chmod(self._tmpdir, 0o755)
        self._closed = False

    @property
    def tmpdir(self) -> str:
        return self._tmpdir

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> "FirejailSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def build_session(config: Optional[dict], cwd: str) -> FirejailSession:
    """Build a :class:`FirejailSession` for the given (already validated) config.

    The working directory ``cwd`` is always whitelisted/writable.  The returned
    session's ``prefix`` is the argv fragment to prepend to the ``pi`` command
    line; the caller is responsible for closing it.
    """
    prefix = list(_HARDENING)
    prefix.append(f"--whitelist={cwd}")

    cfg = config or {}
    for key, flag in (("whitelist", "--whitelist"), ("read_only", "--read-only"),
                      ("blacklist", "--blacklist")):
        for item in cfg.get(key) or []:
            prefix.append(f"{flag}={os.path.expanduser(item)}")

    session = FirejailSession(prefix)

    allow = cfg.get("allow") or []
    if not allow:
        # Hardening only: warn loudly that the network is NOT restricted.
        print(
            "WARNING: firejail network is NOT restricted (no 'allow' entries in config)",
            file=sys.stderr,
        )
        return session

    try:
        iface = cfg.get("iface") or _default_iface()
        if not iface:
            raise ValueError(
                "could not determine the network interface; set 'iface' in the firejail config"
            )

        rules_lines = [
            "*filter",
            ":INPUT DROP [0:0]",
            ":FORWARD DROP [0:0]",
            ":OUTPUT DROP [0:0]",
            "-A INPUT -i lo -j ACCEPT",
            "-A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
            "-A OUTPUT -o lo -j ACCEPT",
        ]
        hosts_lines = ["127.0.0.1 localhost"]

        for entry in allow:
            host = _validate_host(entry["host"])
            port = _validate_port(entry["port"])
            ip = _resolve_ipv4(host)
            rules_lines.append(f"-A OUTPUT -p tcp -d {ip} --dport {port} -j ACCEPT")
            hosts_lines.append(f"{ip} {host}")

        rules_lines.append("COMMIT")

        # IPv6 is not reachable in restricted mode: drop everything except
        # loopback.
        netfilter6_lines = [
            "*filter",
            ":INPUT DROP [0:0]",
            ":FORWARD DROP [0:0]",
            ":OUTPUT DROP [0:0]",
            "-A INPUT -i lo -j ACCEPT",
            "-A OUTPUT -o lo -j ACCEPT",
            "COMMIT",
        ]

        rules_file = os.path.join(session.tmpdir, "netfilter.rules")
        rules6_file = os.path.join(session.tmpdir, "netfilter6.rules")
        hosts_file = os.path.join(session.tmpdir, "hosts")
        _write(rules_file, "\n".join(rules_lines) + "\n")
        _write(rules6_file, "\n".join(netfilter6_lines) + "\n")
        _write(hosts_file, "\n".join(hosts_lines) + "\n")

        session.prefix += [
            f"--net={iface}",
            f"--netfilter={rules_file}",
            f"--netfilter6={rules6_file}",
            f"--hosts-file={hosts_file}",
        ]
        return session
    except Exception:
        session.close()
        raise
