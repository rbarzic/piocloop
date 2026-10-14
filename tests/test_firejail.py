"""Tests for optional Firejail support (src/piocloop/firejail.py).

Only pure helpers and the prefix/rules-file generation are exercised here;
`firejail` itself is never invoked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from piocloop.firejail import FirejailSession, build_session, load_config, strip_jsonc


# ---------------------------------------------------------------------------
# strip_jsonc
# ---------------------------------------------------------------------------


def test_strip_jsonc_comments_and_string_safety():
    text = """
    {
      "name": "wor//ld",          // trailing line comment
      "url": "http://x/y"         // a URL with "//" inside a string
      /* a block
         comment */
    }
    """
    data = json.loads(strip_jsonc(text))
    assert data == {"name": "wor//ld", "url": "http://x/y"}


def test_strip_jsonc_escaped_quote_inside_string():
    # An escaped quote must not end the string, so a URL's "//" survives.
    text = '{"a": "he said \\"hi\\" http://x", "b": 2}'
    assert json.loads(strip_jsonc(text)) == {"a": 'he said "hi" http://x', "b": 2}


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_valid_file(tmp_path):
    cfg = tmp_path / "c.jsonc"
    cfg.write_text(
        '{\n'
        '  "allow": [{"host": "aiserver01.onio.com", "port": 8000}],  // TCP\n'
        '  "whitelist": ["~/.nvm"],\n'
        '  "iface": "eth0"\n'
        '}\n'
    )
    data = load_config(str(cfg))
    assert data["allow"] == [{"host": "aiserver01.onio.com", "port": 8000}]
    assert data["whitelist"] == ["~/.nvm"]
    assert data["iface"] == "eth0"


def test_load_config_rejects_unknown_key(tmp_path):
    cfg = tmp_path / "c.jsonc"
    cfg.write_text('{"bogus": 1}')
    with pytest.raises(ValueError, match="unknown key"):
        load_config(str(cfg))


def test_load_config_rejects_bad_port(tmp_path):
    cfg = tmp_path / "c.jsonc"
    cfg.write_text('{"allow": [{"host": "example.com", "port": 0}]}')
    with pytest.raises(ValueError, match="port"):
        load_config(str(cfg))


def test_load_config_rejects_bad_host(tmp_path):
    cfg = tmp_path / "c.jsonc"
    cfg.write_text('{"allow": [{"host": "bad host!", "port": 80}]}')
    with pytest.raises(ValueError, match="hostname"):
        load_config(str(cfg))


def test_load_config_rejects_invalid_json(tmp_path):
    cfg = tmp_path / "c.jsonc"
    cfg.write_text("{ not json")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_config(str(cfg))


# ---------------------------------------------------------------------------
# build_session / prefix
# ---------------------------------------------------------------------------


def test_prefix_no_config_is_hardening_only(tmp_path, capsys):
    session = build_session(None, cwd=str(tmp_path))
    try:
        assert session.prefix[:5] == [
            "firejail", "--noprofile", "--quiet", "--caps.drop=all", "--nonewprivs",
        ]
        assert f"--whitelist={tmp_path}" in session.prefix
        assert not any(p.startswith("--net=") for p in session.prefix)
        assert not any(p.startswith("--netfilter=") for p in session.prefix)
    finally:
        session.close()
    assert "WARNING" in capsys.readouterr().err


def test_prefix_expands_tilde_and_applies_directory_flags(tmp_path, monkeypatch):
    # Point HOME at a temp dir so ~ expansion is deterministic.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config = {"whitelist": ["~/wl"], "read_only": ["~/ro"], "blacklist": ["/icwork"]}
    session = build_session(config, cwd=str(tmp_path))
    try:
        assert f"--whitelist={home / 'wl'}" in session.prefix
        assert f"--read-only={home / 'ro'}" in session.prefix
        assert "--blacklist=/icwork" in session.prefix
    finally:
        session.close()


def test_prefix_with_allow_writes_rules_and_hosts(tmp_path):
    config = {"allow": [{"host": "localhost", "port": 8000}], "iface": "eth0"}
    session = build_session(config, cwd=str(tmp_path))
    try:
        assert "--net=eth0" in session.prefix

        rules_file = next(p.split("=", 1)[1] for p in session.prefix if p.startswith("--netfilter="))
        rules = Path(rules_file).read_text(encoding="utf-8")
        # default policy is DROP on all chains
        assert ":INPUT DROP [0:0]" in rules
        assert ":FORWARD DROP [0:0]" in rules
        assert ":OUTPUT DROP [0:0]" in rules
        # exactly the expected ACCEPT lines: lo + established/related + the one allow
        accept_lines = [ln for ln in rules.splitlines() if ln.startswith("-A ")]
        assert "-A INPUT -i lo -j ACCEPT" in accept_lines
        assert "-A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT" in accept_lines
        assert "-A OUTPUT -o lo -j ACCEPT" in accept_lines
        assert "-A OUTPUT -p tcp -d 127.0.0.1 --dport 8000 -j ACCEPT" in accept_lines
        assert len([ln for ln in accept_lines if ln.startswith("-A OUTPUT -p tcp")]) == 1

        hosts_file = next(p.split("=", 1)[1] for p in session.prefix if p.startswith("--hosts-file="))
        hosts = Path(hosts_file).read_text(encoding="utf-8")
        assert "127.0.0.1 localhost" in hosts
        assert "127.0.0.1 localhost" in hosts  # the allow entry for localhost too

        # IPv6 drops everything except loopback.
        netfilter_arg = next(p for p in session.prefix if p.startswith("--netfilter="))
        netfilter6_arg = next(p for p in session.prefix if p.startswith("--netfilter6="))
        # --netfilter6= must come right after --netfilter=
        assert session.prefix.index(netfilter6_arg) == session.prefix.index(netfilter_arg) + 1
        rules6 = Path(netfilter6_arg.split("=", 1)[1]).read_text(encoding="utf-8")
        assert rules6.splitlines() == [
            "*filter",
            ":INPUT DROP [0:0]",
            ":FORWARD DROP [0:0]",
            ":OUTPUT DROP [0:0]",
            "-A INPUT -i lo -j ACCEPT",
            "-A OUTPUT -o lo -j ACCEPT",
            "COMMIT",
        ]
    finally:
        session.close()


def test_build_session_allow_requires_iface(tmp_path, monkeypatch):
    # No usable interface: this must raise rather than silently produce a broken sandbox.
    import piocloop.firejail as fj
    monkeypatch.setattr(fj, "_default_iface", lambda: "")
    config = {"allow": [{"host": "localhost", "port": 8000}]}
    with pytest.raises(ValueError, match="iface"):
        build_session(config, cwd=str(tmp_path))


def test_session_is_context_manager(tmp_path):
    with build_session(None, cwd=str(tmp_path)) as session:
        d = session.tmpdir
        assert Path(d).is_dir()
    assert not Path(d).exists()


# ---------------------------------------------------------------------------
# FirejailSession basics
# ---------------------------------------------------------------------------


def test_close_is_idempotent(tmp_path):
    cwd = str(tmp_path)
    session = build_session({"whitelist": []}, cwd=cwd)
    d = session.tmpdir
    assert Path(d).is_dir()
    session.close()
    session.close()  # no double-remove error
    assert not Path(d).exists()
