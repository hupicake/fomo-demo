"""Immutable, vendored source baseline for generated Next.js projects.

The starter is source-controlled with the control plane and baked into the
OpenSandbox image.  It is intentionally not a shadcn registry client: the
model receives the available imports but never performs an install or upgrade.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath

from fomo.sandbox.base import FileChange

STARTER_ID = "fomo-next-radix-v1"
STARTER_VERSION = "1.0.0"
_EXPECTED_TREE_SHA256 = "07cd7372813569fd270f04a11a219ad4fc200d3a21274ac677fd85b3da1d32d1"

# These paths are maintained by the runner rather than an Architect or an
# Engineer. They remain present in the public manifest so the Architect has a
# complete no-write boundary.
_SYSTEM_PROTECTED_PATHS = (
    ".gitignore",
    "package-lock.json",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "playwright.config.ts",
)
_STARTER_PROTECTED_PATHS = (
    "package.json",
    "pnpm-lock.yaml",
    "components.json",
    "next.config.ts",
    "next-env.d.ts",
    "tsconfig.json",
    "postcss.config.mjs",
    "app/layout.tsx",
    "app/globals.css",
    "lib/utils.ts",
    "components/ui/**",
)
_MODEL_OWNED_ROOTS = (
    "app/page.tsx",
    "app/(generated)/**",
    "components/features/**",
    "lib/domain/**",
    "tests/**",
)


class StarterIntegrityError(RuntimeError):
    """A sandbox did not receive the exact immutable starter source."""


@dataclass(frozen=True, slots=True)
class StarterFile:
    path: str
    sha256: str
    size: int
    ownership: str
    _content: bytes = field(repr=False)

    def as_manifest_entry(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "ownership": self.ownership,
        }

    def as_change(self) -> FileChange:
        return FileChange(path=self.path, content=self._content.decode("utf-8"), operation="create")


@dataclass(frozen=True, slots=True)
class StarterManifest:
    """A reproducible source tree and its model-visible write contract."""

    id: str
    version: str
    tree_sha256: str
    files: tuple[StarterFile, ...]
    available_imports: tuple[str, ...]
    protected_paths: tuple[str, ...]
    model_owned_roots: tuple[str, ...]
    base_scripts: dict[str, str]

    @property
    def file_changes(self) -> list[FileChange]:
        return [entry.as_change() for entry in self.files]

    def is_protected_path(self, path: str) -> bool:
        return any(_matches_path(pattern, path) for pattern in self.protected_paths)

    def is_model_owned_path(self, path: str) -> bool:
        return any(_matches_path(pattern, path) for pattern in self.model_owned_roots)

    def verify_file(self, path: str, content: bytes) -> None:
        expected = next((entry for entry in self.files if entry.path == path), None)
        if expected is None:
            raise StarterIntegrityError("starter verification referenced an unknown file")
        if len(content) != expected.size or hashlib.sha256(content).hexdigest() != expected.sha256:
            raise StarterIntegrityError(f"starter file verification failed for {path}")

    def verify_tree(self, copied_files: dict[str, bytes]) -> None:
        if set(copied_files) != {entry.path for entry in self.files}:
            raise StarterIntegrityError("starter file set verification failed")
        verified_entries: list[StarterFile] = []
        for entry in self.files:
            content = copied_files[entry.path]
            self.verify_file(entry.path, content)
            verified_entries.append(
                StarterFile(
                    path=entry.path,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size=len(content),
                    ownership=entry.ownership,
                    _content=content,
                )
            )
        if _tree_sha256(verified_entries) != self.tree_sha256:
            raise StarterIntegrityError("starter tree verification failed")

    def as_architect_context(self) -> dict[str, object]:
        """Return only the compact immutable contract, never source bodies."""
        return {
            "id": self.id,
            "version": self.version,
            "treeSha256": self.tree_sha256,
            "availableImports": list(self.available_imports),
            "protectedPaths": list(self.protected_paths),
            "modelOwnedRoots": list(self.model_owned_roots),
            "baseScripts": dict(self.base_scripts),
        }

    def as_provenance(self, initial_commit_sha: str) -> dict[str, object]:
        return {
            **self.as_architect_context(),
            "initialCommitSha": initial_commit_sha,
            "files": [entry.as_manifest_entry() for entry in self.files],
        }


def default_starter_manifest() -> StarterManifest:
    """Load the single supported starter and calculate its canonical digest."""
    entries = tuple(_load_asset_files())
    tree_sha256 = _tree_sha256(entries)
    if tree_sha256 != _EXPECTED_TREE_SHA256:
        raise StarterIntegrityError(
            "starter assets differ from the pinned fomo-next-radix-v1 tree digest"
        )
    package = next(entry for entry in entries if entry.path == "package.json")
    package_json = json.loads(package._content.decode("utf-8"))
    scripts = package_json.get("scripts")
    if not isinstance(scripts, dict) or not all(
        isinstance(scripts.get(name), str) for name in ("dev", "build", "typecheck", "test:smoke")
    ):
        raise StarterIntegrityError("starter package.json is missing required scripts")
    available_imports = tuple(
        f"@/components/ui/{PurePosixPath(entry.path).stem}"
        for entry in entries
        if entry.path.startswith("components/ui/") and entry.path.endswith(".tsx")
    )
    return StarterManifest(
        id=STARTER_ID,
        version=STARTER_VERSION,
        tree_sha256=tree_sha256,
        files=entries,
        available_imports=available_imports,
        protected_paths=(*_SYSTEM_PROTECTED_PATHS, *_STARTER_PROTECTED_PATHS),
        model_owned_roots=_MODEL_OWNED_ROOTS,
        base_scripts={name: scripts[name] for name in ("dev", "build", "typecheck", "test:smoke")},
    )


def _load_asset_files() -> list[StarterFile]:
    root = resources.files("fomo").joinpath("starter_assets").joinpath(STARTER_ID)
    files = sorted(_walk_resources(root), key=lambda item: item[0])
    if not files:
        raise StarterIntegrityError("starter assets are missing")
    return [
        StarterFile(
            path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            ownership=(
                "model"
                if _matches_path("app/page.tsx", path)
                else "system"
                if path == "playwright.config.ts"
                else "starter"
            ),
            _content=content,
        )
        for path, content in files
    ]


def _walk_resources(directory: Traversable, prefix: str = "") -> list[tuple[str, bytes]]:
    result: list[tuple[str, bytes]] = []
    for child in directory.iterdir():
        path = f"{prefix}{child.name}"
        if child.is_dir():
            result.extend(_walk_resources(child, f"{path}/"))
        elif child.is_file():
            normalized = PurePosixPath(path)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise StarterIntegrityError("starter asset path is invalid")
            result.append((str(normalized), child.read_bytes()))
    return result


def _tree_sha256(entries: tuple[StarterFile, ...] | list[StarterFile]) -> str:
    canonical = "".join(
        f"{entry.path}\0{entry.sha256}\0{entry.size}\n" for entry in sorted(entries, key=lambda item: item.path)
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _matches_path(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return path == pattern
