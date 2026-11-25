"""Loads config.toml (falls back to config.example.toml defaults)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass
class GmailConfig:
    enabled: bool = True
    credentials_path: Path = ROOT / "credentials.json"
    token_path: Path = ROOT / "token.json"
    poll_minutes: int = 15
    query: str = "from:(messages-noreply@linkedin.com) newer_than:30d"
    subject_patterns: list[str] = field(default_factory=list)
    from_name_suffix: str = " via LinkedIn"


@dataclass
class Config:
    my_name: str = "Me"
    db_path: Path = ROOT / "tracker.db"
    follow_up_days: int = 4
    capture_port: int = 8765
    shared_secret: str = ""
    anthropic_api_key_path: Path | None = None
    vision_model: str = "claude-opus-5-5"
    gmail: GmailConfig = field(default_factory=GmailConfig)

    def anthropic_api_key(self) -> str | None:
        """Key from the configured file; None lets the SDK use its own credential chain."""
        if self.anthropic_api_key_path and self.anthropic_api_key_path.exists():
            return self.anthropic_api_key_path.read_text().strip() or None
        return None


def _resolve(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(os.path.expanduser(value))
    return p if p.is_absolute() else base / p


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("TRACKER_CONFIG") or ROOT / "config.toml")
    if not path.exists():
        path = ROOT / "config.example.toml"
    base = path.parent
    raw = tomllib.loads(path.read_text())
    g = raw.get("gmail", {})
    gmail = GmailConfig(
        enabled=g.get("enabled", True),
        credentials_path=_resolve(base, g.get("credentials_path", "credentials.json")),
        token_path=_resolve(base, g.get("token_path", "token.json")),
        poll_minutes=int(g.get("poll_minutes", 15)),
        query=g.get("query", GmailConfig.query),
        subject_patterns=list(g.get("subject_patterns", [])),
        from_name_suffix=g.get("from_name_suffix", " via LinkedIn"),
    )
    return Config(
        my_name=raw.get("my_name", "Me"),
        db_path=_resolve(base, raw.get("db_path", "tracker.db")),
        follow_up_days=int(raw.get("follow_up_days", 4)),
        capture_port=int(raw.get("capture_port", 8765)),
        shared_secret=raw.get("shared_secret", ""),
        anthropic_api_key_path=_resolve(base, raw.get("anthropic_api_key_path")),
        vision_model=raw.get("vision_model", "claude-opus-5-5"),
        gmail=gmail,
    )
