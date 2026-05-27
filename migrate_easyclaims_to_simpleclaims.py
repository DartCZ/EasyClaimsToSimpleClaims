#!/usr/bin/env python3
"""Convert EasyClaims JSON data into a SimpleClaims SQLite database.

The converter targets the storage layout used by:

* EasyClaims as checked out in F:/Hytale/EasyClaims on 2026-05-27.
* SimpleClaims 1.0.38 / commit aa198e4 as checked out in F:/Hytale/SimpleClaims.

Run the server without EasyClaims after copying the generated database into
<universe>/SimpleClaims/SimpleClaims.db.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import math
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ADMIN_UUID = str(uuid.UUID(int=0))
INT_MAX = 2_147_483_647
PARTY_NAMESPACE = uuid.UUID("c9ca41f2-6435-44de-995d-f51485d5a747")

P_CLAIM_BASE = "simpleclaims.claim.base"
P_PLACE = "simpleclaims.party.protection.place_blocks"
P_BREAK = "simpleclaims.party.protection.break_blocks"
P_INTERACT = "simpleclaims.party.protection.interact"
P_PVP = "simpleclaims.party.protection.pvp"
P_ALLOW_ENTRY = "simpleclaims.party.protection.allow_entry"
P_CHEST = "simpleclaims.party.protection.interact.chest"
P_DOOR = "simpleclaims.party.protection.interact.door"
P_BENCH = "simpleclaims.party.protection.interact.bench"
P_CHAIR = "simpleclaims.party.protection.interact.chair"
P_PORTAL = "simpleclaims.party.protection.interact.portal"
P_TAMED_DAMAGE = "simpleclaims.party.protection.tamed_damage"

TRUST_ORDER = {
    "none": 0,
    "use": 1,
    "container": 2,
    "workstation": 3,
    "damage": 4,
    "build": 5,
}

BASE_OVERRIDES = {
    P_PLACE: False,
    P_BREAK: False,
    P_INTERACT: False,
    P_CHEST: False,
    P_DOOR: False,
    P_BENCH: False,
    P_CHAIR: False,
    P_PORTAL: False,
    P_ALLOW_ENTRY: True,
    # EasyClaims does not prevent damage to tamed entities.
    P_TAMED_DAMAGE: True,
}

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE parties (
    id TEXT PRIMARY KEY,
    owner TEXT,
    name TEXT,
    description TEXT,
    color INTEGER,
    created_user_uuid TEXT,
    created_user_name TEXT,
    created_date TEXT,
    modified_user_uuid TEXT,
    modified_user_name TEXT,
    modified_date TEXT
);
CREATE TABLE party_members (
    party_id TEXT,
    member_uuid TEXT,
    PRIMARY KEY (party_id, member_uuid),
    FOREIGN KEY (party_id) REFERENCES parties(id) ON DELETE CASCADE
);
CREATE TABLE party_overrides (
    party_id TEXT,
    type TEXT,
    value_type TEXT,
    value TEXT,
    PRIMARY KEY (party_id, type),
    FOREIGN KEY (party_id) REFERENCES parties(id) ON DELETE CASCADE
);
CREATE TABLE party_allies (
    party_id TEXT,
    ally_party_id TEXT,
    PRIMARY KEY (party_id, ally_party_id),
    FOREIGN KEY (party_id) REFERENCES parties(id) ON DELETE CASCADE
);
CREATE TABLE player_allies (
    party_id TEXT,
    player_uuid TEXT,
    PRIMARY KEY (party_id, player_uuid),
    FOREIGN KEY (party_id) REFERENCES parties(id) ON DELETE CASCADE
);
CREATE TABLE claims (
    dimension TEXT,
    chunkX INTEGER,
    chunkZ INTEGER,
    party_owner TEXT,
    created_user_uuid TEXT,
    created_user_name TEXT,
    created_date TEXT,
    PRIMARY KEY (dimension, chunkX, chunkZ)
);
CREATE TABLE name_cache (
    uuid TEXT PRIMARY KEY,
    name TEXT,
    last_seen INTEGER DEFAULT -1,
    play_time REAL DEFAULT 0
);
CREATE TABLE admin_overrides (
    uuid TEXT PRIMARY KEY
);
CREATE TABLE party_permission_overrides (
    party_id TEXT,
    target_uuid TEXT,
    permission TEXT,
    value INTEGER,
    PRIMARY KEY (party_id, target_uuid, permission),
    FOREIGN KEY (party_id) REFERENCES parties(id) ON DELETE CASCADE
);
CREATE TABLE reserved_chunks (
    dimension TEXT,
    chunkX INTEGER,
    chunkZ INTEGER,
    reserved_by TEXT,
    PRIMARY KEY (dimension, chunkX, chunkZ)
);
"""


class MigrationError(RuntimeError):
    pass


@dataclass
class Trust:
    player_id: str
    name: str
    level: str


@dataclass
class SourceClaim:
    owner_id: str
    world: str
    chunk_x: int
    chunk_z: int
    claimed_at: int = 0
    pvp_enabled: bool = True
    admin_claim: bool = False
    display_name: str | None = None
    recovered_from_index: bool = False

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.world, self.chunk_x, self.chunk_z)


@dataclass
class SourcePlayer:
    player_id: str
    claims: list[SourceClaim] = field(default_factory=list)
    trusted: dict[str, Trust] = field(default_factory=dict)
    bonus_claim_slots: int = 0
    bonus_max_claims: int = 0
    unlimited_claims: bool = False
    playtime_seconds: int = 0


@dataclass
class Party:
    party_id: str
    owner_id: str
    owner_name: str
    name: str
    description: str
    color: int
    claims: list[SourceClaim]
    members: list[str]
    overrides: dict[str, tuple[str, str]]
    trusted: dict[str, Trust] = field(default_factory=dict)


@dataclass
class Report:
    warnings: list[str] = field(default_factory=list)
    player_parties: int = 0
    admin_parties: int = 0
    claims: int = 0
    allies: int = 0
    permission_rows: int = 0
    recovered_index_claims: int = 0
    over_limit_parties: int = 0
    damage_trusts_downgraded: int = 0

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    def as_dict(self) -> dict[str, Any]:
        return {
            "player_parties": self.player_parties,
            "admin_parties": self.admin_parties,
            "claims": self.claims,
            "allies": self.allies,
            "permission_rows": self.permission_rows,
            "recovered_index_claims": self.recovered_index_claims,
            "over_limit_parties": self.over_limit_parties,
            "damage_trusts_downgraded": self.damage_trusts_downgraded,
            "warnings": self.warnings,
        }


def canonical_uuid(value: Any, context: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise MigrationError(f"Invalid UUID in {context}: {value!r}") from exc


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationError(f"Invalid JSON in {path}: {exc}") from exc


def locate_source(path: Path) -> tuple[Path, Path]:
    path = path.resolve()
    if (path / "claims").is_dir():
        return path, path / "claims"
    if path.name.lower() == "claims" and path.is_dir():
        return path.parent, path
    raise MigrationError(
        f"{path} is not an EasyClaims data directory; expected a child directory named claims."
    )


def load_names(claims_dir: Path, report: Report) -> dict[str, str]:
    names_path = claims_dir / "names.json"
    if not names_path.exists():
        report.warn("EasyClaims names.json was not found; UUID-based fallback names will be used.")
        return {}
    raw = read_json(names_path)
    if not isinstance(raw, dict):
        raise MigrationError(f"{names_path} must contain a JSON object.")
    names: dict[str, str] = {}
    for raw_id, raw_name in raw.items():
        player_id = canonical_uuid(raw_id, str(names_path))
        if isinstance(raw_name, str) and raw_name:
            names[player_id] = raw_name
    return names


def parse_trusts(data: dict[str, Any], source_file: Path) -> dict[str, Trust]:
    trusts: dict[str, Trust] = {}
    modern = data.get("trustedPlayersData")
    previous = data.get("trustedPlayersWithNames")
    oldest = data.get("trustedPlayers")
    if isinstance(modern, dict):
        for raw_id, raw_trust in modern.items():
            player_id = canonical_uuid(raw_id, str(source_file))
            entry = raw_trust if isinstance(raw_trust, dict) else {}
            name = entry.get("name") or player_id
            level = str(entry.get("level") or "build").lower().strip()
            if level not in TRUST_ORDER:
                level = "build"
            trusts[player_id] = Trust(player_id, str(name), level)
    elif isinstance(previous, dict):
        for raw_id, raw_name in previous.items():
            player_id = canonical_uuid(raw_id, str(source_file))
            trusts[player_id] = Trust(player_id, str(raw_name or player_id), "build")
    elif isinstance(oldest, list):
        for raw_id in oldest:
            player_id = canonical_uuid(raw_id, str(source_file))
            trusts[player_id] = Trust(player_id, player_id, "build")
    return trusts


def load_players(data_dir: Path, claims_dir: Path, report: Report) -> dict[str, SourcePlayer]:
    players: dict[str, SourcePlayer] = {}
    for source_file in sorted(claims_dir.glob("*.json")):
        if source_file.name.lower() in {"index.json", "names.json"}:
            continue
        try:
            player_id = canonical_uuid(source_file.stem, str(source_file))
        except MigrationError:
            report.warn(f"Skipped non-player JSON file in claims directory: {source_file.name}")
            continue
        raw = read_json(source_file)
        if not isinstance(raw, dict):
            raise MigrationError(f"{source_file} must contain a JSON object.")
        player = SourcePlayer(
            player_id=player_id,
            trusted=parse_trusts(raw, source_file),
            bonus_claim_slots=int(raw.get("bonusClaimSlots") or 0),
            bonus_max_claims=int(raw.get("bonusMaxClaims") or 0),
            unlimited_claims=bool(raw.get("unlimitedClaims", False)),
        )
        for raw_claim in raw.get("claims") or []:
            if not isinstance(raw_claim, dict):
                continue
            try:
                claim = SourceClaim(
                    owner_id=player_id,
                    world=str(raw_claim["world"]),
                    chunk_x=int(raw_claim["chunkX"]),
                    chunk_z=int(raw_claim["chunkZ"]),
                    claimed_at=int(raw_claim.get("claimedAt") or 0),
                    pvp_enabled=bool(
                        True if raw_claim.get("pvpEnabled") is None else raw_claim["pvpEnabled"]
                    ),
                    admin_claim=bool(raw_claim.get("adminClaim", False)),
                    display_name=raw_claim.get("displayName"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MigrationError(f"Malformed claim in {source_file}: {raw_claim!r}") from exc
            player.claims.append(claim)
        playtime_path = data_dir / "playtime" / f"{player_id}.json"
        if playtime_path.exists():
            raw_playtime = read_json(playtime_path)
            if isinstance(raw_playtime, dict):
                player.playtime_seconds = int(raw_playtime.get("totalPlaytimeSeconds") or 0)
        players[player_id] = player
    return players


def load_config(data_dir: Path) -> dict[str, Any]:
    config = {
        "startingClaims": 4,
        "claimsPerHour": 2.0,
        "maxClaims": 50,
        "pvpInPlayerClaims": True,
    }
    path = data_dir / "config.json"
    if not path.exists():
        return config
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise MigrationError(f"{path} must contain a JSON object.")
    aliases = {
        "startingChunks": "startingClaims",
        "chunksPerHour": "claimsPerHour",
        "maxClaimsPerPlayer": "maxClaims",
    }
    for old_key, new_key in aliases.items():
        if new_key not in raw and old_key in raw:
            raw[new_key] = raw[old_key]
    if "allowPlayerPvpToggle" in raw and "pvpInPlayerClaims" not in raw:
        raw["pvpInPlayerClaims"] = not bool(raw["allowPlayerPvpToggle"])
    for key in config:
        if key in raw:
            config[key] = raw[key]
    return config


def load_claims(
    claims_dir: Path, players: dict[str, SourcePlayer], report: Report
) -> list[SourceClaim]:
    candidates: dict[tuple[str, int, int], list[SourceClaim]] = {}
    for player in players.values():
        for claim in player.claims:
            candidates.setdefault(claim.key, []).append(claim)

    index_path = claims_dir / "index.json"
    indexed: dict[tuple[str, int, int], str] = {}
    if index_path.exists():
        raw_index = read_json(index_path)
        if not isinstance(raw_index, dict):
            raise MigrationError(f"{index_path} must contain a JSON object.")
        for world, raw_chunks in raw_index.items():
            if not isinstance(raw_chunks, dict):
                continue
            for coordinates, raw_owner in raw_chunks.items():
                try:
                    chunk_x, chunk_z = (int(value) for value in coordinates.split(",", 1))
                except (ValueError, AttributeError) as exc:
                    raise MigrationError(
                        f"Invalid chunk coordinate key in {index_path}: {coordinates!r}"
                    ) from exc
                indexed[(str(world), chunk_x, chunk_z)] = canonical_uuid(raw_owner, str(index_path))

    claims: list[SourceClaim] = []
    all_keys = set(candidates) | set(indexed)
    for key in sorted(all_keys):
        options = candidates.get(key, [])
        indexed_owner = indexed.get(key)
        if indexed_owner is not None:
            matching = [claim for claim in options if claim.owner_id == indexed_owner]
            if matching:
                selected = matching[0]
            else:
                selected = SourceClaim(
                    owner_id=indexed_owner,
                    world=key[0],
                    chunk_x=key[1],
                    chunk_z=key[2],
                    pvp_enabled=False if indexed_owner == ADMIN_UUID else True,
                    admin_claim=indexed_owner == ADMIN_UUID,
                    display_name="Server" if indexed_owner == ADMIN_UUID else None,
                    recovered_from_index=True,
                )
                report.recovered_index_claims += 1
                report.warn(
                    f"Recovered indexed claim {key} for {indexed_owner}; no matching player claim record existed."
                )
            if options and not matching:
                report.warn(
                    f"Index owner wins for claim {key}: {indexed_owner}; JSON listed another owner."
                )
        else:
            if len({claim.owner_id for claim in options}) > 1:
                raise MigrationError(
                    f"Conflicting JSON claims at {key} and no index entry identifies the owner."
                )
            selected = options[0]
            report.warn(f"Claim {key} is present in a player file but missing from index.json; retained.")
        claims.append(selected)
    return claims


def owner_name(player_id: str, names: dict[str, str]) -> str:
    if player_id == ADMIN_UUID:
        return "Server"
    return names.get(player_id, player_id[:8])


def party_id_for_player(player_id: str) -> str:
    return str(uuid.uuid5(PARTY_NAMESPACE, f"player:{player_id}"))


def party_id_for_admin(name: str, pvp_enabled: bool) -> str:
    return str(uuid.uuid5(PARTY_NAMESPACE, f"admin:{name}:{str(pvp_enabled).lower()}"))


def fake_owner_for_admin(party_id: str) -> str:
    return str(uuid.uuid5(PARTY_NAMESPACE, f"admin-owner:{party_id}"))


def stable_color(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535.0
    red, green, blue = (round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 0.70, 0.95))
    packed = (255 << 24) | (red << 16) | (green << 8) | blue
    return packed - (1 << 32) if packed >= (1 << 31) else packed


def iso_date(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return "1970-01-01T00:00:00"
    return (
        datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="milliseconds")
    )


def protection_overrides(pvp_enabled: bool) -> dict[str, tuple[str, str]]:
    overrides = {
        key: ("bool", str(value).lower()) for key, value in BASE_OVERRIDES.items()
    }
    overrides[P_PVP] = ("bool", str(pvp_enabled).lower())
    return overrides


def easy_capacity(player: SourcePlayer, config: dict[str, Any]) -> int:
    if player.unlimited_claims:
        return INT_MAX
    starting = max(0, int(config["startingClaims"]))
    per_hour = max(0.0, float(config["claimsPerHour"]))
    maximum = max(1, int(config["maxClaims"]))
    from_playtime = math.floor((player.playtime_seconds / 3600.0) * per_hour)
    cap = maximum + player.bonus_max_claims
    return max(0, min(starting + from_playtime, cap) + player.bonus_claim_slots)


def allowed_permissions(level: str, damage_as_break: bool) -> set[str]:
    rank = TRUST_ORDER[level]
    # Allies take the permission branch before public party defaults in
    # SimpleClaims, so preserve EasyClaims' unrestricted entry/tamed damage.
    permissions: set[str] = {P_ALLOW_ENTRY, P_TAMED_DAMAGE}
    if rank >= TRUST_ORDER["use"]:
        permissions.update({P_INTERACT, P_DOOR, P_CHAIR, P_PORTAL})
    if rank >= TRUST_ORDER["container"]:
        permissions.add(P_CHEST)
    if rank >= TRUST_ORDER["workstation"]:
        permissions.add(P_BENCH)
    if rank >= TRUST_ORDER["build"]:
        permissions.update({P_PLACE, P_BREAK})
    elif level == "damage" and damage_as_break:
        permissions.add(P_BREAK)
    return permissions


def build_parties(
    players: dict[str, SourcePlayer],
    claims: list[SourceClaim],
    names: dict[str, str],
    config: dict[str, Any],
    preserve_limits: bool,
    damage_as_break: bool,
    report: Report,
) -> list[Party]:
    claims_by_owner: dict[str, list[SourceClaim]] = {}
    admin_groups: dict[tuple[str, bool], list[SourceClaim]] = {}
    for claim in claims:
        if claim.admin_claim or claim.owner_id == ADMIN_UUID:
            name = str(claim.display_name or "Server")
            admin_groups.setdefault((name, claim.pvp_enabled), []).append(claim)
        else:
            claims_by_owner.setdefault(claim.owner_id, []).append(claim)
            players.setdefault(claim.owner_id, SourcePlayer(claim.owner_id))

    parties: list[Party] = []
    for player_id in sorted(player_id for player_id in players if player_id != ADMIN_UUID):
        player = players[player_id]
        player_claims = claims_by_owner.get(player_id, [])
        overrides = protection_overrides(bool(config["pvpInPlayerClaims"]))
        capacity = easy_capacity(player, config)
        if preserve_limits:
            overrides[P_CLAIM_BASE] = ("integer", str(capacity))
        if len(player_claims) > capacity and not player.unlimited_claims:
            report.over_limit_parties += 1
            report.warn(
                f"{owner_name(player_id, names)} has {len(player_claims)} claims but EasyClaims capacity {capacity}; "
                "claims are retained and new claiming remains limited."
            )
        party = Party(
            party_id=party_id_for_player(player_id),
            owner_id=player_id,
            owner_name=owner_name(player_id, names),
            name=f"{owner_name(player_id, names)}'s Party",
            description="Migrated from EasyClaims",
            color=stable_color(player_id),
            claims=player_claims,
            members=[player_id],
            overrides=overrides,
            trusted=player.trusted,
        )
        parties.append(party)
        report.player_parties += 1
        for trust in player.trusted.values():
            if trust.level == "none" or trust.player_id == party.owner_id:
                continue
            report.allies += 1
            report.permission_rows += len(allowed_permissions(trust.level, damage_as_break))
            if trust.level == "damage" and not damage_as_break:
                report.damage_trusts_downgraded += 1
                report.warn(
                    f"Trust level damage for {trust.name} in {party.name} omits block breaking; "
                    "rerun with --damage-as-break to grant the SimpleClaims approximation."
                )

    for (name, pvp_enabled), admin_claims in sorted(admin_groups.items()):
        party_id = party_id_for_admin(name, pvp_enabled)
        overrides = protection_overrides(pvp_enabled)
        overrides[P_CLAIM_BASE] = ("integer", str(max(1, len(admin_claims))))
        parties.append(
            Party(
                party_id=party_id,
                owner_id=fake_owner_for_admin(party_id),
                owner_name="Server",
                name=name,
                description="Migrated EasyClaims admin claim",
                color=stable_color(f"admin:{name}:{pvp_enabled}"),
                claims=admin_claims,
                members=[],
                overrides=overrides,
            )
        )
        report.admin_parties += 1
    report.claims = len(claims)
    return parties


def backup_and_prepare_output(output: Path, overwrite: bool) -> Path | None:
    if not output.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
        return None
    if not overwrite:
        raise MigrationError(
            f"Output database already exists: {output}. Use --overwrite to replace it with a backup."
        )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = output.with_name(f"{output.name}.backup-{stamp}")
    shutil.copy2(output, backup)
    output.unlink()
    return backup


def write_database(
    output: Path,
    parties: list[Party],
    players: dict[str, SourcePlayer],
    names: dict[str, str],
    damage_as_break: bool,
    overwrite: bool,
    report: Report,
) -> Path | None:
    backup = backup_and_prepare_output(output, overwrite)
    connection = sqlite3.connect(output)
    try:
        connection.executescript(SCHEMA)
        with connection:
            for party in parties:
                earliest = min(
                    (claim.claimed_at for claim in party.claims if claim.claimed_at > 0),
                    default=0,
                )
                created_date = iso_date(earliest)
                connection.execute(
                    "INSERT INTO parties VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        party.party_id,
                        party.owner_id,
                        party.name,
                        party.description,
                        party.color,
                        party.owner_id,
                        party.owner_name,
                        created_date,
                        party.owner_id,
                        party.owner_name,
                        created_date,
                    ),
                )
                connection.executemany(
                    "INSERT INTO party_members (party_id, member_uuid) VALUES (?, ?)",
                    [(party.party_id, member) for member in party.members],
                )
                connection.executemany(
                    "INSERT INTO party_overrides (party_id, type, value_type, value) VALUES (?, ?, ?, ?)",
                    [
                        (party.party_id, key, value_type, value)
                        for key, (value_type, value) in party.overrides.items()
                    ],
                )
                for claim in party.claims:
                    creator_id = claim.owner_id if claim.owner_id != ADMIN_UUID else party.owner_id
                    creator_name = owner_name(claim.owner_id, names)
                    connection.execute(
                        "INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            claim.world,
                            claim.chunk_x,
                            claim.chunk_z,
                            party.party_id,
                            creator_id,
                            creator_name,
                            iso_date(claim.claimed_at),
                        ),
                    )
                for trust in party.trusted.values():
                    if trust.level == "none" or trust.player_id == party.owner_id:
                        continue
                    connection.execute(
                        "INSERT OR IGNORE INTO player_allies (party_id, player_uuid) VALUES (?, ?)",
                        (party.party_id, trust.player_id),
                    )
                    permissions = allowed_permissions(trust.level, damage_as_break)
                    connection.executemany(
                        "INSERT INTO party_permission_overrides "
                        "(party_id, target_uuid, permission, value) VALUES (?, ?, ?, 1)",
                        [(party.party_id, trust.player_id, permission) for permission in permissions],
                    )

            cached_names = dict(names)
            for player_id in players:
                cached_names.setdefault(player_id, owner_name(player_id, names))
            for player in players.values():
                for trust in player.trusted.values():
                    cached_names.setdefault(trust.player_id, trust.name)
            cached_names.pop(ADMIN_UUID, None)
            connection.executemany(
                "INSERT INTO name_cache (uuid, name, last_seen, play_time) VALUES (?, ?, -1, 0)",
                sorted(cached_names.items()),
            )
    finally:
        connection.close()
    return backup


def output_summary(report: Report, output: Path | None, backup: Path | None, dry_run: bool) -> None:
    action = "Dry run" if dry_run else f"Created {output}"
    print(action)
    print(
        f"Player parties: {report.player_parties}; admin parties: {report.admin_parties}; "
        f"claims: {report.claims}; trusted allies: {report.allies}."
    )
    if backup is not None:
        print(f"Backup of previous output: {backup}")
    if report.warnings:
        print(f"Warnings: {len(report.warnings)}")
        for warning in report.warnings:
            print(f"- {warning}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate EasyClaims JSON data to a SimpleClaims SQLite database."
    )
    parser.add_argument(
        "--easyclaims-data",
        required=True,
        type=Path,
        help="EasyClaims plugin data directory containing claims/ (or the claims/ directory itself).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Target SimpleClaims.db path. Required unless --dry-run is used.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output database after making a timestamped backup.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report the migration without writing a database.",
    )
    parser.add_argument(
        "--no-limit-overrides",
        action="store_true",
        help="Do not store EasyClaims calculated claim capacities as SimpleClaims party base limits.",
    )
    parser.add_argument(
        "--damage-as-break",
        action="store_true",
        help="Map EasyClaims damage trust to SimpleClaims block breaking (less restrictive approximation).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Write a JSON migration report to this path.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.dry_run and args.output is None:
        parser.error("--output is required unless --dry-run is used")
    return args


def migrate(args: argparse.Namespace) -> tuple[Report, Path | None]:
    report = Report()
    data_dir, claims_dir = locate_source(args.easyclaims_data)
    names = load_names(claims_dir, report)
    players = load_players(data_dir, claims_dir, report)
    config = load_config(data_dir)
    claims = load_claims(claims_dir, players, report)
    parties = build_parties(
        players,
        claims,
        names,
        config,
        preserve_limits=not args.no_limit_overrides,
        damage_as_break=args.damage_as_break,
        report=report,
    )
    backup = None
    if not args.dry_run:
        backup = write_database(
            args.output.resolve(),
            parties,
            players,
            names,
            args.damage_as_break,
            args.overwrite,
            report,
        )
    if args.report:
        args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.report.resolve().write_text(
            json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8"
        )
    return report, backup


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report, backup = migrate(args)
        output_summary(report, args.output.resolve() if args.output else None, backup, args.dry_run)
        return 0
    except MigrationError as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
