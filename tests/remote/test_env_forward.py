"""Env forwarding: value stays in memory, never on the remote disk."""

from __future__ import annotations

from acestor.remote.env_forward import compose_env_prefix


def test_empty_when_no_vars_set(monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAR", raising=False)
    assert compose_env_prefix(["FOO", "BAR"]) == ""


def test_composes_env_prefix_for_set_vars(monkeypatch):
    monkeypatch.setenv("FOO", "hello")
    monkeypatch.setenv("BAR", "world value")
    prefix = compose_env_prefix(["FOO", "BAR"])
    assert prefix.startswith("env ")
    assert prefix.endswith(" ")
    # BAR contains a space → must be quoted for the remote shell
    assert "BAR='world value'" in prefix
    assert "FOO=hello" in prefix


def test_skips_unset_vars_silently(monkeypatch):
    monkeypatch.setenv("HAVE", "x")
    monkeypatch.delenv("MISSING", raising=False)
    prefix = compose_env_prefix(["HAVE", "MISSING"])
    assert "HAVE=x" in prefix
    assert "MISSING" not in prefix


def test_quotes_values_with_shell_metacharacters(monkeypatch):
    monkeypatch.setenv("TRICKY", "a'b\"c $d")
    prefix = compose_env_prefix(["TRICKY"])
    # shlex.quote wraps in single quotes and escapes any internal '
    assert "TRICKY='a'\"'\"'b\"c $d'" in prefix
