"""Resolve Yahoo teams to people using data/managers.yaml plus sensible fallbacks."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

HIDDEN = {"", "--", "--hidden--", "hidden"}


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "manager"


def load_config(path: Path) -> dict:
    if not path.exists():
        return {"managers": [], "lore": []}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("managers", [])
    data.setdefault("lore", [])
    data["managers"] = data["managers"] or []
    data["lore"] = data["lore"] or []
    return data


class Resolver:
    """Maps team rows -> manager ids. Explicit team overrides beat GUID matches beat auto ids."""

    def __init__(self, config: dict, team_rows: list[dict]):
        self.managers: dict[str, dict] = {}
        self.by_guid: dict[str, str] = {}
        self.by_team: dict[str, str] = {}
        for m in config.get("managers", []):
            mid = str(m.get("id") or slugify(m.get("name", "")))
            self.managers[mid] = {
                "id": mid,
                "name": m.get("name") or mid,
                "nickname": m.get("nickname") or "",
                "bio": m.get("bio") or "",
                "lore": m.get("lore") or [],
                "active": m.get("active"),
            }
            for guid in m.get("guids") or []:
                self.by_guid[str(guid)] = mid
            for team in m.get("teams") or []:
                self.by_team[str(team)] = mid

        self.team_to_manager: dict[str, str] = {}
        hidden_counter = 0
        for t in sorted(team_rows, key=lambda r: (r["season"], r["team_id"])):
            override = self.by_team.get(f"{t['season']}:{t['team_id']}")
            guid = t.get("manager_guid") or ""
            nick = (t.get("manager_nickname") or "").strip()
            if override:
                mid = override
            elif guid and guid in self.by_guid:
                mid = self.by_guid[guid]
            elif nick.lower() not in HIDDEN:
                mid = self._auto(slugify(nick), nick, guid)
            elif guid:
                hidden_counter += 1
                mid = self._auto(f"hidden-{guid[:6].lower()}", f"Mystery Manager {hidden_counter}", guid)
            else:
                mid = self._auto(f"team-{t['season']}-{t['team_id']}", t.get("name") or "Unknown", "")
            self.team_to_manager[t["team_key"]] = mid

    def _auto(self, base: str, name: str, guid: str) -> str:
        mid = base
        n = 2
        while mid in self.managers and guid and self.by_guid.get(guid) != mid:
            mid = f"{base}-{n}"
            n += 1
        if mid not in self.managers:
            self.managers[mid] = {"id": mid, "name": name, "nickname": "", "bio": "", "lore": [], "active": None}
        if guid:
            self.by_guid[guid] = mid
        return mid

    def manager_for(self, team_key: str) -> str | None:
        return self.team_to_manager.get(team_key)
