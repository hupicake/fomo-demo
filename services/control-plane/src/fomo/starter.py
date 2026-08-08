"""Digest-pinned, composable Golden Starter source for generated projects.

The control plane owns the selected base and capability overlays. Models see a
compact catalog and may select only its fixed identifiers; they never install
dependencies, name an image path, or write an immutable starter asset.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath

from fomo.sandbox.base import FileChange

STARTER_ID = "fomo-next-radix-v2"
STARTER_VERSION = "2.0.0"
_BASE_ASSET_DIRECTORY = "base"
_CAPABILITY_ASSET_DIRECTORY = "capabilities"

# These constants are intentionally source-pinned rather than recalculated as
# a trust decision. They are filled from the checked-in files below; changing
# any base/capability asset therefore requires an explicit versioned update.
_EXPECTED_BASE_TREE_SHA256 = (
    "f5197e083a8c1724783e0cf4a6ec913ef2eb2a241d7db92e3d54f85198ad9dfb"
)
_EXPECTED_CAPABILITY_TREE_SHA256 = {
    "crud": "1d1bb2d5e289051e8b1d812215da5bb8965b9454f48205787f7eebb72ea2cbad",
    "local-persistence": "9f1f18947005981cce5c6dc67ea38b6ca96af19e0c798f2e9f5249e6592337d1",
}

# These paths are maintained by the runner rather than an Architect or an
# Engineer. They remain present in the public manifest so the Architect has a
# complete no-write boundary.
_SYSTEM_PROTECTED_PATHS = (
    ".gitignore",
    "package-lock.json",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
)
_BASE_STARTER_PROTECTED_PATHS = (
    "package.json",
    "pnpm-lock.yaml",
    "components.json",
    "next.config.ts",
    "next-env.d.ts",
    "tsconfig.json",
    "postcss.config.mjs",
    "playwright.config.ts",
    "app/layout.tsx",
    "app/page.tsx",
    "app/error.tsx",
    "app/loading.tsx",
    "app/globals.css",
    "lib/utils.ts",
    "components/ui/**",
    "components/system/**",
    "tests/harness/**",
)
_MODEL_OWNED_ROOTS = (
    "app/(generated)/**",
    "components/features/**",
    "lib/domain/**",
    "tests/generated/**",
)
_BASE_AVAILABLE_IMPORTS = (
    "@/components/system/app-shell",
    "@/components/system/feedback",
)
_FORBIDDEN_MODEL_OWNED_PATHS = ("app/(generated)/page.tsx",)
_REQUIRED_SCRIPTS = ("dev", "build", "typecheck", "test:smoke")
_VALIDATION_COMMANDS = ("pnpm typecheck", "pnpm build", "pnpm test:smoke")


class StarterIntegrityError(RuntimeError):
    """A requested starter selection or copied source failed closed."""


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
        return FileChange(
            path=self.path,
            content=self._content.decode("utf-8"),
            operation="create",
        )


@dataclass(frozen=True, slots=True)
class StarterCapability:
    """One fixed asset overlay that an Architect may select by identifier."""

    id: str
    version: str
    tree_sha256: str
    files: tuple[StarterFile, ...]
    available_imports: tuple[str, ...]
    protected_paths: tuple[str, ...]
    description: str
    provides: tuple[str, ...]
    conflicts: tuple[str, ...] = ()

    def as_architect_context(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "treeSha256": self.tree_sha256,
            "availableImports": list(self.available_imports),
            "protectedPaths": list(self.protected_paths),
            "conflicts": list(self.conflicts),
            "description": self.description,
            "provides": list(self.provides),
        }

    def as_provenance_entry(self) -> dict[str, str]:
        return {
            "id": self.id,
            "version": self.version,
            "treeSha256": self.tree_sha256,
        }


@dataclass(frozen=True, slots=True)
class StarterExtensionContract:
    """One fixed model-owned port that keeps the protected root page stable."""

    path: str
    operation: str
    export_style: str
    symbol: str
    purpose: str

    def as_architect_context(self) -> dict[str, str]:
        return {
            "path": self.path,
            "operation": self.operation,
            "exportStyle": self.export_style,
            "symbol": self.symbol,
            "purpose": self.purpose,
        }


_ROOT_EXTENSION_CONTRACT = StarterExtensionContract(
    path="app/(generated)/composition.tsx",
    operation="modify",
    export_style="named",
    symbol="GeneratedComposition",
    purpose="Replace the neutral root composition behind the protected app/page.tsx delegation.",
)


@dataclass(frozen=True, slots=True)
class StarterValidationVariant:
    """Declared verification matrix; the control plane never runs it implicitly."""

    name: str
    capability_ids: tuple[str, ...]
    commands: tuple[str, ...] = _VALIDATION_COMMANDS


@dataclass(frozen=True, slots=True)
class StarterManifest:
    """A reproducible base-plus-capabilities source tree and write contract."""

    id: str
    version: str
    tree_sha256: str
    file_tree_sha256: str
    files: tuple[StarterFile, ...]
    available_imports: tuple[str, ...]
    protected_paths: tuple[str, ...]
    model_owned_roots: tuple[str, ...]
    forbidden_model_owned_paths: tuple[str, ...]
    base_scripts: dict[str, str]
    base_tree_sha256: str
    selected_capabilities: tuple[StarterCapability, ...]
    capability_catalog: tuple[StarterCapability, ...]
    root_extension_contract: StarterExtensionContract

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(capability.id for capability in self.selected_capabilities)

    @property
    def file_changes(self) -> list[FileChange]:
        return [entry.as_change() for entry in self.files]

    def is_protected_path(self, path: str) -> bool:
        return any(_matches_path(pattern, path) for pattern in self.protected_paths)

    def is_model_owned_path(self, path: str) -> bool:
        return any(_matches_path(pattern, path) for pattern in self.model_owned_roots)

    def is_forbidden_model_owned_path(self, path: str) -> bool:
        return path in self.forbidden_model_owned_paths

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
        if _tree_sha256(verified_entries) != self.file_tree_sha256:
            raise StarterIntegrityError("starter tree verification failed")

    def as_architect_context(self) -> dict[str, object]:
        """Return only the compact immutable contract, never source bodies."""
        return {
            "id": self.id,
            "version": self.version,
            "treeSha256": self.tree_sha256,
            "base": {
                "id": self.id,
                "version": self.version,
                "treeSha256": self.base_tree_sha256,
            },
            "capabilityCatalog": [
                capability.as_architect_context() for capability in self.capability_catalog
            ],
            "selectedCapabilities": [
                capability.as_architect_context() for capability in self.selected_capabilities
            ],
            "availableImports": list(self.available_imports),
            "protectedPaths": list(self.protected_paths),
            "modelOwnedRoots": list(self.model_owned_roots),
            "forbiddenModelOwnedPaths": list(self.forbidden_model_owned_paths),
            "extensionContracts": [self.root_extension_contract.as_architect_context()],
            "baseScripts": dict(self.base_scripts),
        }

    def as_provenance(self, initial_commit_sha: str) -> dict[str, object]:
        context = self.as_architect_context()
        context.update(
            {
                "selectedCapabilities": [
                    capability.as_provenance_entry() for capability in self.selected_capabilities
                ],
                "initialCommitSha": initial_commit_sha,
                "files": [entry.as_manifest_entry() for entry in self.files],
            }
        )
        return context


def default_starter_manifest() -> StarterManifest:
    """Return the digest-pinned bare Golden Starter v2."""
    return resolve_starter_manifest(())


def capability_catalog() -> tuple[StarterCapability, ...]:
    """Load and independently verify every approved capability asset tree."""
    catalog = (
        _load_capability(
            "crud",
            version="1.0.0",
            available_imports=("@/components/starter/crud-slots",),
            protected_paths=("components/starter/crud-slots.tsx",),
            description="Reusable client-side collection state and rendering boundaries.",
            provides=("collection state", "create/update/remove actions", "render slots"),
        ),
        _load_capability(
            "local-persistence",
            version="1.0.0",
            available_imports=("@/lib/starter/local-persistence",),
            protected_paths=("lib/starter/local-persistence.ts",),
            description="A browser-only persistence boundary with explicit validation and migration.",
            provides=("SSR-safe localStorage access", "typed versioned envelopes", "migration adapter"),
        ),
    )
    if len({capability.id for capability in catalog}) != len(catalog):
        raise StarterIntegrityError("capability catalog contains duplicate identifiers")
    return catalog


def resolve_starter_manifest(
    capability_ids: Iterable[str],
    *,
    catalog: Sequence[StarterCapability] | None = None,
) -> StarterManifest:
    """Resolve a fixed, order-independent selection into one immutable manifest."""
    requested = tuple(str(capability_id) for capability_id in capability_ids)
    if len(requested) != len(set(requested)):
        raise StarterIntegrityError("duplicate capability selection")

    available_catalog = tuple(catalog) if catalog is not None else capability_catalog()
    catalog_by_id = {capability.id: capability for capability in available_catalog}
    if len(catalog_by_id) != len(available_catalog):
        raise StarterIntegrityError("capability catalog contains duplicate identifiers")
    unknown = sorted(set(requested) - set(catalog_by_id))
    if unknown:
        raise StarterIntegrityError(f"unknown capability selection: {', '.join(unknown)}")

    selected = tuple(
        sorted(
            (catalog_by_id[capability_id] for capability_id in requested),
            key=lambda item: item.id,
        )
    )
    _validate_capability_selection(selected)

    base_files, base_scripts, base_tree_sha256 = _load_base()
    files = _combine_files(base_files, selected)
    return StarterManifest(
        id=STARTER_ID,
        version=STARTER_VERSION,
        tree_sha256=_composite_tree_sha256(base_tree_sha256, selected),
        file_tree_sha256=_tree_sha256(files),
        files=files,
        available_imports=tuple(
            dict.fromkeys(
                (
                    *_base_available_imports(base_files),
                    *(item for cap in selected for item in cap.available_imports),
                )
            )
        ),
        protected_paths=(
            *_SYSTEM_PROTECTED_PATHS,
            *_BASE_STARTER_PROTECTED_PATHS,
            *(item for cap in selected for item in cap.protected_paths),
        ),
        model_owned_roots=_MODEL_OWNED_ROOTS,
        forbidden_model_owned_paths=_FORBIDDEN_MODEL_OWNED_PATHS,
        base_scripts=base_scripts,
        base_tree_sha256=base_tree_sha256,
        selected_capabilities=selected,
        capability_catalog=available_catalog,
        root_extension_contract=_ROOT_EXTENSION_CONTRACT,
    )


def starter_validation_variants() -> tuple[StarterValidationVariant, ...]:
    """List the explicit one-shot seed checks without executing them here."""
    return (
        StarterValidationVariant(name="bare", capability_ids=()),
        StarterValidationVariant(name="crud", capability_ids=("crud",)),
        StarterValidationVariant(name="local-persistence", capability_ids=("local-persistence",)),
        StarterValidationVariant(
            name="crud-local-persistence", capability_ids=("crud", "local-persistence")
        ),
    )


def _load_base() -> tuple[tuple[StarterFile, ...], dict[str, str], str]:
    entries = tuple(_load_asset_files((_BASE_ASSET_DIRECTORY,), ownership="starter"))
    tree_sha256 = _tree_sha256(entries)
    if tree_sha256 != _EXPECTED_BASE_TREE_SHA256:
        raise StarterIntegrityError(
            "starter base assets differ from the pinned fomo-next-radix-v2 tree digest"
        )
    package = next((entry for entry in entries if entry.path == "package.json"), None)
    if package is None:
        raise StarterIntegrityError("starter base is missing package.json")
    try:
        package_json = json.loads(package._content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StarterIntegrityError("starter package.json is invalid") from exc
    scripts = package_json.get("scripts")
    if not isinstance(scripts, dict) or not all(
        isinstance(scripts.get(name), str) for name in _REQUIRED_SCRIPTS
    ):
        raise StarterIntegrityError("starter package.json is missing required scripts")
    return entries, {name: scripts[name] for name in _REQUIRED_SCRIPTS}, tree_sha256


def _load_capability(
    capability_id: str,
    *,
    version: str,
    available_imports: tuple[str, ...],
    protected_paths: tuple[str, ...],
    description: str,
    provides: tuple[str, ...],
) -> StarterCapability:
    entries = tuple(
        _load_asset_files((_CAPABILITY_ASSET_DIRECTORY, capability_id), ownership="capability")
    )
    tree_sha256 = _tree_sha256(entries)
    expected = _EXPECTED_CAPABILITY_TREE_SHA256[capability_id]
    if tree_sha256 != expected:
        raise StarterIntegrityError(
            f"starter capability assets differ from pinned {capability_id} tree digest"
        )
    expected_paths = {entry.path for entry in entries}
    if set(protected_paths) != expected_paths:
        raise StarterIntegrityError(
            f"starter capability {capability_id} protected paths do not match assets"
        )
    return StarterCapability(
        id=capability_id,
        version=version,
        tree_sha256=tree_sha256,
        files=entries,
        available_imports=available_imports,
        protected_paths=protected_paths,
        description=description,
        provides=provides,
    )


def _combine_files(
    base_files: tuple[StarterFile, ...], selected: tuple[StarterCapability, ...]
) -> tuple[StarterFile, ...]:
    files = list(base_files)
    known_paths = {entry.path for entry in files}
    for capability in selected:
        capability_paths = {entry.path for entry in capability.files}
        if len(capability_paths) != len(capability.files) or known_paths.intersection(
            capability_paths
        ):
            raise StarterIntegrityError("overlay path collision")
        known_paths.update(capability_paths)
        files.extend(capability.files)
    return tuple(sorted(files, key=lambda entry: entry.path))


def _validate_capability_selection(selected: tuple[StarterCapability, ...]) -> None:
    selected_ids = {capability.id for capability in selected}
    for capability in selected:
        conflicts = selected_ids.intersection(capability.conflicts)
        if conflicts:
            raise StarterIntegrityError("conflicting capabilities selected")


def _base_available_imports(entries: tuple[StarterFile, ...]) -> tuple[str, ...]:
    ui_imports = tuple(
        f"@/components/ui/{PurePosixPath(entry.path).stem}"
        for entry in entries
        if entry.path.startswith("components/ui/") and entry.path.endswith(".tsx")
    )
    return (*_BASE_AVAILABLE_IMPORTS, *ui_imports)


def _load_asset_files(path_parts: tuple[str, ...], *, ownership: str) -> list[StarterFile]:
    root = resources.files("fomo").joinpath("starter_assets").joinpath(STARTER_ID)
    for part in path_parts:
        root = root.joinpath(part)
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
                if ownership == "starter" and _matches_any(_MODEL_OWNED_ROOTS, path)
                else "system"
                if ownership == "starter" and path in _SYSTEM_PROTECTED_PATHS
                else ownership
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
        f"{entry.path}\0{entry.sha256}\0{entry.size}\n"
        for entry in sorted(entries, key=lambda item: item.path)
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _composite_tree_sha256(base_tree_sha256: str, selected: tuple[StarterCapability, ...]) -> str:
    canonical = (
        f"base\0{STARTER_ID}\0{STARTER_VERSION}\0{base_tree_sha256}\n"
        + "".join(
            f"capability\0{capability.id}\0{capability.version}\0{capability.tree_sha256}\n"
            for capability in sorted(selected, key=lambda item: item.id)
        )
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _matches_any(patterns: tuple[str, ...], path: str) -> bool:
    return any(_matches_path(pattern, path) for pattern in patterns)


def _matches_path(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return path == pattern
