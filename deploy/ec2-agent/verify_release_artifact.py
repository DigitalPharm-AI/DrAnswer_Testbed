from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = "release-manifest.json"
RELEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}")
REQUIRED_MEMBERS = {
    "agent_app/main.py",
    "agent_app/migrate.py",
    "agent_app/worker_main.py",
    "scripts/backfill_agent_embeddings.py",
    "scripts/backfill_symptom_concepts.py",
    "deploy/ec2-agent/requirements.agent.lock",
    "deploy/ec2-agent/systemd/dranswer-agent-api.service",
    "deploy/ec2-agent/systemd/dranswer-agent-worker.service",
    "deploy/ec2-agent/systemd/dranswer-agent-migrate.service",
}


def _safe_member_name(name: str) -> str:
    normalized = PurePosixPath(name)
    if (
        normalized.is_absolute()
        or not normalized.parts
        or any(part in {"", ".", ".."} for part in normalized.parts)
    ):
        raise ValueError("archive contains an unsafe path")
    return normalized.as_posix()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_manifest(
    manifest: Any,
    *,
    seen: set[str],
    member_payloads: dict[str, bytes],
) -> dict[str, Any]:
    missing = sorted(REQUIRED_MEMBERS - seen)
    if missing:
        raise ValueError(
            "archive is missing required members: " + ", ".join(missing)
        )
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported release manifest schema")

    release_id = str(manifest.get("release_id") or "")
    commit_sha = str(manifest.get("commit_sha") or "")
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise ValueError("release manifest contains an invalid release ID")
    if not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError("release manifest contains an invalid commit SHA")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("kind") != "git_archive":
        raise ValueError("release manifest source must be git_archive")
    if source.get("clean_commit_required") is not True:
        raise ValueError("release manifest must require a clean commit")
    file_hashes = source.get("file_sha256")
    if not isinstance(file_hashes, dict) or not file_hashes:
        raise ValueError("release manifest has no source file hashes")
    critical_hashes = source.get("critical_sha256")
    if not isinstance(critical_hashes, dict) or not critical_hashes:
        raise ValueError("release manifest has no critical source hashes")
    if not set(critical_hashes) <= set(file_hashes):
        raise ValueError("critical source hashes are not in the source file set")
    for member_name, expected_hash in file_hashes.items():
        if not isinstance(member_name, str) or not isinstance(
            expected_hash,
            str,
        ):
            raise ValueError("release manifest source hash is invalid")
        payload = member_payloads.get(member_name)
        if payload is None:
            raise ValueError(
                f"release source member is missing: {member_name}"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("release manifest source hash is malformed")
        if not hmac.compare_digest(_sha256(payload), expected_hash):
            raise ValueError(
                f"release source member hash mismatch: {member_name}"
            )
    return manifest


def verify_release_artifact(path: Path) -> dict[str, Any]:
    seen: set[str] = set()
    member_payloads: dict[str, bytes] = {}
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive.getmembers():
            name = _safe_member_name(member.name)
            if name in seen:
                raise ValueError("archive contains a duplicate path")
            seen.add(name)
            if member.isdev() or member.issym() or member.islnk():
                raise ValueError("archive contains an unsupported special file")
            if member.isfile():
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError("archive member cannot be read")
                member_payloads[name] = extracted.read()
    try:
        manifest = json.loads(member_payloads[MANIFEST_NAME])
    except KeyError as exc:
        raise ValueError("release manifest is missing") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("release manifest is invalid") from exc
    return _validate_manifest(
        manifest,
        seen=seen,
        member_payloads=member_payloads,
    )


def verify_installed_release(path: Path) -> dict[str, Any]:
    if not path.is_dir():
        raise ValueError("installed release directory is missing")
    manifest_path = path / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("installed release manifest is invalid") from exc
    seen: set[str] = set()
    member_payloads: dict[str, bytes] = {}
    for relative_name in REQUIRED_MEMBERS | {
        str(name)
        for name in (
            manifest.get("source", {}).get("file_sha256", {})
            if isinstance(manifest, dict)
            and isinstance(manifest.get("source"), dict)
            else {}
        )
    }:
        relative_path = path / _safe_member_name(relative_name)
        if not relative_path.is_file() or relative_path.is_symlink():
            raise ValueError(
                f"installed release member is missing: {relative_name}"
            )
        seen.add(relative_name)
        member_payloads[relative_name] = relative_path.read_bytes()
    seen.add(MANIFEST_NAME)
    member_payloads[MANIFEST_NAME] = manifest_path.read_bytes()
    return _validate_manifest(
        manifest,
        seen=seen,
        member_payloads=member_payloads,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify an Agent release archive and print safe metadata."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--archive", type=Path)
    group.add_argument("--installed-dir", type=Path)
    args = parser.parse_args()
    try:
        manifest = (
            verify_release_artifact(args.archive)
            if args.archive is not None
            else verify_installed_release(args.installed_dir)
        )
    except (OSError, tarfile.TarError, ValueError) as exc:
        print(
            f"ERROR: invalid Agent release artifact: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"{manifest['release_id']} {manifest['commit_sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
