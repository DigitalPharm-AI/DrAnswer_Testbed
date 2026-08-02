from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

BUNDLE_PATHS = (
    "agent_app",
    "shared",
    "data/pro_ctcae_korean_parsed.xlsx",
    "scripts/backfill_agent_embeddings.py",
    "scripts/backfill_symptom_concepts.py",
    "requirements.txt",
    "pyproject.toml",
    "deploy/ec2-agent",
)
CRITICAL_SOURCE_FILES = (
    "deploy/ec2-agent/requirements.agent.lock",
    "deploy/ec2-agent/.env.agent_app.ec2.example",
    "agent_app/llm/prompts.py",
    "shared/backend_read_contract.py",
    "data/pro_ctcae_korean_parsed.xlsx",
    "scripts/backfill_agent_embeddings.py",
    "scripts/backfill_symptom_concepts.py",
)
MANIFEST_NAME = "release-manifest.json"
RELEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
PRIVATE_KEY_PATTERN = re.compile(
    r"(?i)(?:\.(?:pem|ppk|key|p12|pfx)|/(?:id_rsa|id_ed25519))$"
)
COMMON_SECRET_TEMPLATE_KEYS = {
    "INTERNAL_API_TOKEN",
    "AGENT_SYNC_API_TOKEN",
    "AGENT_FEEDBACK_ENCRYPTION_KEY",
    "LANGFUSE_SECRET_KEY",
}


def _git(repo_root: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def assert_clean_repository(repo_root: Path) -> str:
    commit_sha = str(_git(repo_root, "rev-parse", "--verify", "HEAD")).strip()
    dirty = str(
        _git(
            repo_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
    ).strip()
    if dirty:
        raise RuntimeError(
            "release bundle requires a clean Git commit; "
            "commit or remove every tracked and untracked change first"
        )
    return commit_sha


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_config_identifiers(template: bytes) -> dict[str, str]:
    allowed = {
        "APP_ENV",
        "LLM_PROVIDER",
        "LLM_MODEL_TIER",
        "LLM_FAST_MODEL",
        "LLM_SONNET_MODEL",
        "LLM_REASONING_ENABLED",
        "LLM_REASONING_EFFORT",
    }
    identifiers: dict[str, str] = {}
    for raw_line in template.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in allowed:
            identifiers[key.lower()] = value.strip()
    return identifiers


def _validate_bedrock_templates(
    common_template: bytes,
    bedrock_template: bytes,
) -> None:
    common_text = common_template.decode("utf-8")
    if re.search(
        r"(?m)^\s*(?:export\s+)?AWS_BEARER_TOKEN_BEDROCK\s*=",
        common_text,
    ):
        raise ValueError(
            "common EC2 environment template contains a Bedrock credential key"
        )
    common_values: dict[str, str] = {}
    for raw_line in common_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        common_values[key.strip()] = value.strip().strip("\"'")
    for key in sorted(COMMON_SECRET_TEMPLATE_KEYS):
        value = common_values.get(key)
        if value is None:
            raise ValueError(
                f"common EC2 environment template is missing {key}"
            )
        if value and "CHANGE_ME" not in value:
            raise ValueError(
                f"common EC2 environment template contains a value for {key}"
            )
    assignments = re.findall(
        r"(?m)^\s*(?:export\s+)?AWS_BEARER_TOKEN_BEDROCK\s*=(.*)$",
        bedrock_template.decode("utf-8"),
    )
    if len(assignments) != 1 or assignments[0].strip():
        raise ValueError(
            "Bedrock environment template must contain one empty token assignment"
        )


def _manifest(
    *,
    commit_sha: str,
    release_id: str,
    commit_timestamp: int,
    source_payloads: dict[str, bytes],
) -> dict[str, Any]:
    file_hashes = {
        path: _sha256(payload)
        for path, payload in sorted(source_payloads.items())
    }
    source_hashes = {
        path: file_hashes[path]
        for path in CRITICAL_SOURCE_FILES
    }
    config_template = source_payloads[
        "deploy/ec2-agent/.env.agent_app.ec2.example"
    ]
    bedrock_template = source_payloads[
        "deploy/ec2-agent/.env.bedrock.ec2.example"
    ]
    _validate_bedrock_templates(config_template, bedrock_template)
    return {
        "schema_version": 1,
        "release_id": release_id,
        "commit_sha": commit_sha,
        "commit_timestamp": commit_timestamp,
        "source": {
            "kind": "git_archive",
            "clean_commit_required": True,
            "paths": list(BUNDLE_PATHS),
            "file_sha256": file_hashes,
            "critical_sha256": source_hashes,
        },
        "artifact": {
            "format": "tar.gz",
            "deterministic_metadata": True,
            "builder": "deploy/ec2-agent/build_release_bundle.py",
        },
        "runtime_identifiers": {
            **_safe_config_identifiers(config_template),
            "prompt_source": "agent_app/llm/prompts.py",
            "prompt_source_sha256": source_hashes[
                "agent_app/llm/prompts.py"
            ],
            "config_template_sha256": source_hashes[
                "deploy/ec2-agent/.env.agent_app.ec2.example"
            ],
            "dependency_lock_sha256": source_hashes[
                "deploy/ec2-agent/requirements.agent.lock"
            ],
            "backend_read_contract_source_sha256": source_hashes[
                "shared/backend_read_contract.py"
            ],
            "pro_ctcae_reference_sha256": source_hashes[
                "data/pro_ctcae_korean_parsed.xlsx"
            ],
        },
    }


def _normalized_member(
    member: tarfile.TarInfo,
    *,
    commit_timestamp: int,
) -> tarfile.TarInfo:
    normalized = tarfile.TarInfo(PurePosixPath(member.name).as_posix())
    normalized.mode = member.mode
    normalized.type = member.type
    normalized.linkname = member.linkname
    normalized.size = member.size
    normalized.mtime = commit_timestamp
    normalized.uid = 0
    normalized.gid = 0
    normalized.uname = "root"
    normalized.gname = "root"
    return normalized


def _forbidden_archive_member(name: str) -> bool:
    normalized = PurePosixPath(name).as_posix()
    basename = PurePosixPath(normalized).name
    runtime_env = (
        (basename == ".env" or basename.startswith(".env."))
        and not basename.endswith(".example")
    ) or basename in {"agent.env", "bedrock.env"}
    return runtime_env or bool(PRIVATE_KEY_PATTERN.search(f"/{normalized}"))


def build_release_bundle(
    *,
    repo_root: Path,
    output_directory: Path,
    release_id: str = "",
) -> tuple[Path, str, dict[str, Any]]:
    repo_root = repo_root.resolve()
    commit_sha = assert_clean_repository(repo_root)
    effective_release_id = release_id.strip() or f"git-{commit_sha[:12]}"
    if not RELEASE_ID_PATTERN.fullmatch(effective_release_id):
        raise ValueError(
            "release ID must match [A-Za-z0-9][A-Za-z0-9._-]{0,79}"
        )
    commit_timestamp = int(
        str(_git(repo_root, "show", "-s", "--format=%ct", commit_sha)).strip()
    )
    archive_bytes = _git(
        repo_root,
        "archive",
        "--format=tar",
        commit_sha,
        "--",
        *BUNDLE_PATHS,
        text=False,
    )
    assert isinstance(archive_bytes, bytes)
    source_payloads: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as source:
        for member in source.getmembers():
            if not member.isfile():
                continue
            extracted = source.extractfile(member)
            if extracted is not None:
                source_payloads[member.name] = extracted.read()
    missing_critical = sorted(
        set(CRITICAL_SOURCE_FILES) - set(source_payloads)
    )
    if missing_critical:
        raise RuntimeError(
            "Git archive is missing critical release files: "
            + ", ".join(missing_critical)
        )
    manifest = _manifest(
        commit_sha=commit_sha,
        release_id=effective_release_id,
        commit_timestamp=commit_timestamp,
        source_payloads=source_payloads,
    )
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    output_directory.mkdir(parents=True, exist_ok=True)
    archive_path = (
        output_directory / f"dranswer-agent-{effective_release_id}.tar.gz"
    )
    with archive_path.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_output,
            mtime=commit_timestamp,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as destination:
                with tarfile.open(
                    fileobj=io.BytesIO(archive_bytes),
                    mode="r:",
                ) as source:
                    for member in sorted(
                        source.getmembers(),
                        key=lambda item: item.name,
                    ):
                        if _forbidden_archive_member(member.name):
                            raise RuntimeError(
                                "release archive contains a runtime environment "
                                "file or private key"
                            )
                        extracted = source.extractfile(member)
                        destination.addfile(
                            _normalized_member(
                                member,
                                commit_timestamp=commit_timestamp,
                            ),
                            extracted,
                        )
                manifest_member = tarfile.TarInfo(MANIFEST_NAME)
                manifest_member.mode = 0o644
                manifest_member.size = len(manifest_bytes)
                manifest_member.mtime = commit_timestamp
                manifest_member.uid = 0
                manifest_member.gid = 0
                manifest_member.uname = "root"
                manifest_member.gname = "root"
                destination.addfile(
                    manifest_member,
                    io.BytesIO(manifest_bytes),
                )

    archive_sha256 = _sha256(archive_path.read_bytes())
    checksum_path = Path(f"{archive_path}.sha256")
    checksum_path.write_text(
        f"{archive_sha256}  {archive_path.name}\n",
        encoding="ascii",
        newline="\n",
    )
    return archive_path, archive_sha256, manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic Agent release from one clean Git commit."
        )
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--release-id", default="")
    args = parser.parse_args()
    archive_path, archive_sha256, manifest = build_release_bundle(
        repo_root=args.repo_root,
        output_directory=args.output_directory,
        release_id=args.release_id,
    )
    print(
        json.dumps(
            {
                "archive": str(archive_path),
                "sha256": archive_sha256,
                "release_id": manifest["release_id"],
                "commit_sha": manifest["commit_sha"],
                "checksum_file": f"{archive_path}.sha256",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
