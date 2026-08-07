from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from opensandbox.exceptions import SandboxApiException
from pydantic import ValidationError

from fomo.config import DEFAULT_OPENSANDBOX_IMAGE, Settings
from fomo.sandbox import create_sandbox_provider
from fomo.sandbox.base import Command, FileChange, SandboxPathError, SourceRef
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.sandbox.opensandbox import OpenSandboxProvider
from fomo.sandbox.process import ProcessSandboxProvider
from fomo.schemas import ProductSpec


def test_product_spec_requires_unique_acceptance_ids() -> None:
    with pytest.raises(ValidationError):
        ProductSpec.model_validate(
            {
                "title": "Duplicate ACs",
                "problem": "test",
                "visualDirection": {"tone": "clear"},
                "acceptanceCriteria": [
                    {"id": "AC-1", "given": "a", "when": "b", "then": "c"},
                    {"id": "AC-1", "given": "d", "when": "e", "then": "f"},
                ],
            }
        )


def test_opensandbox_image_defaults_to_curated_base_and_stays_overridable(monkeypatch) -> None:
    monkeypatch.delenv("OPENSANDBOX_IMAGE", raising=False)
    assert Settings.from_env().opensandbox_image == DEFAULT_OPENSANDBOX_IMAGE
    monkeypatch.setenv("OPENSANDBOX_IMAGE", "registry.example/fomo:custom")
    assert Settings.from_env().opensandbox_image == "registry.example/fomo:custom"


def test_opensandbox_lifetime_defaults_stays_overridable_and_rejects_invalid_values(monkeypatch) -> None:
    monkeypatch.delenv("OPENSANDBOX_LIFETIME_SECONDS", raising=False)
    assert Settings.from_env().opensandbox_lifetime_seconds == 21_600

    monkeypatch.setenv("OPENSANDBOX_LIFETIME_SECONDS", "3600")
    assert Settings.from_env().opensandbox_lifetime_seconds == 3600

    for value in ("0", "-1", "21601"):
        monkeypatch.setenv("OPENSANDBOX_LIFETIME_SECONDS", value)
        with pytest.raises(ValueError, match="OPENSANDBOX_LIFETIME_SECONDS"):
            Settings.from_env()


def test_opensandbox_lifetime_is_forwarded_by_factory(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class CapturingOpenSandboxProvider:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("fomo.sandbox.OpenSandboxProvider", CapturingOpenSandboxProvider)

    create_sandbox_provider(Settings(opensandbox_lifetime_seconds=1234))

    assert captured["lifetime_seconds"] == 1234


def test_sandbox_proxy_settings_are_explicit_and_do_not_inherit_host_proxy(monkeypatch) -> None:
    for name in ("SANDBOX_HTTP_PROXY", "SANDBOX_HTTPS_PROXY", "SANDBOX_NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://host-proxy.invalid:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://host-proxy.invalid:7890")

    assert Settings.from_env().sandbox_proxy_environment == {}

    monkeypatch.setenv("SANDBOX_HTTP_PROXY", "http://host.docker.internal:7890")
    monkeypatch.setenv("SANDBOX_HTTPS_PROXY", "https://proxy.example.test:8443")
    monkeypatch.setenv("SANDBOX_NO_PROXY", "localhost,127.0.0.1,.internal")

    assert Settings.from_env().sandbox_proxy_environment == {
        "HTTP_PROXY": "http://host.docker.internal:7890",
        "HTTPS_PROXY": "https://proxy.example.test:8443",
        "NO_PROXY": "localhost,127.0.0.1,.internal",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SANDBOX_HTTP_PROXY", "ftp://proxy.example.test:21"),
        ("SANDBOX_HTTP_PROXY", "http://user:password@proxy.example.test:7890"),
        ("SANDBOX_HTTPS_PROXY", "https:///missing-host"),
        ("SANDBOX_HTTP_PROXY", "http://proxy.example.test:99999"),
    ],
)
def test_sandbox_proxy_settings_reject_unsafe_urls(monkeypatch, name: str, value: str) -> None:
    for environment_name in ("SANDBOX_HTTP_PROXY", "SANDBOX_HTTPS_PROXY", "SANDBOX_NO_PROXY"):
        monkeypatch.delenv(environment_name, raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="proxy"):
        Settings.from_env()


@pytest.mark.asyncio
async def test_fake_sandbox_contract() -> None:
    provider = FakeSandboxProvider()
    ref = await provider.create("project-1")
    await provider.apply_changes(ref, [FileChange(path="src/app.ts", content="export {}")])
    assert await provider.read_file(ref, "src/app.ts") == b"export {}"
    result = await provider.exec(ref, Command("echo test"), lambda _stream, _text: _noop())
    assert result.exit_code == 0
    assert (await provider.expose(ref, 8080)).url == "http://fake-preview.invalid:8080"
    assert (await provider.snapshot(ref)).location == "fake://snapshot"
    with pytest.raises(SandboxPathError):
        await provider.apply_changes(ref, [FileChange(path="../escape", content="no")])
    with pytest.raises(SandboxPathError):
        await provider.apply_changes(ref, [FileChange(path=".envrc", content="no")])


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_opensandbox_read_file_normalizes_sdk_not_found() -> None:
    class Files:
        async def create_directories(self, _entries) -> None:
            return None

        async def read_bytes(self, _path: str) -> bytes:
            raise SandboxApiException("not found", status_code=404)

    class Handle:
        id = "server-sandbox-not-found"
        files = Files()

        async def destroy(self) -> None:
            return None

    class SDK:
        @classmethod
        async def create(cls, _image, **_kwargs):
            return Handle()

    provider = OpenSandboxProvider("http://sandbox.test", sandbox_class=SDK)
    ref = await provider.create("project-not-found")

    with pytest.raises(FileNotFoundError, match=".gitignore"):
        await provider.read_file(ref, ".gitignore")


@pytest.mark.asyncio
async def test_process_sandbox_contract(tmp_path) -> None:
    provider = ProcessSandboxProvider(tmp_path / "sandbox-root", enabled=True, default_timeout_seconds=5)
    ref = await provider.create("project-1")
    await provider.apply_changes(
        ref,
        [
            FileChange(path="hello.txt", content="hello"),
            FileChange(path="node_modules/pkg/index.js", content="dependency"),
            FileChange(path="playwright-report/index.html", content="report"),
        ],
    )
    output: list[str] = []

    async def sink(_stream: str, text: str) -> None:
        output.append(text)

    result = await provider.exec(ref, Command("cat hello.txt", timeout_seconds=5), sink)
    assert result.exit_code == 0
    assert "hello" in "".join(output)
    files = await provider.list_files(ref)
    assert files[0]["path"] == "hello.txt"
    assert (await provider.snapshot(ref)).location
    await provider.kill(ref)


@pytest.mark.asyncio
async def test_opensandbox_provider_uses_pinned_sdk_contract_and_application_port() -> None:
    class Files:
        def __init__(self) -> None:
            self.directories: list[str] = []
            self.directory_entries = []
            self.write_entries = []
            self.delete_calls: list[list[str]] = []
            self.files: dict[str, bytes] = {
                "/workspace/old.txt": b"remove me",
                "/workspace/node_modules/pkg/index.js": b"dependency",
                "/workspace/playwright-report/index.html": b"report",
            }

        async def create_directories(self, entries) -> None:
            self.directory_entries.extend(entries)
            self.directories.extend(entry.path for entry in entries)

        async def write_files(self, entries) -> None:
            self.write_entries.extend(entries)
            for entry in entries:
                data = entry.data or ""
                self.files[entry.path] = data.encode() if isinstance(data, str) else data

        async def delete_files(self, paths) -> None:
            paths = list(paths)
            self.delete_calls.append(paths)
            for path in paths:
                self.files.pop(path, None)

        async def read_bytes(self, path: str) -> bytes:
            return self.files[path]

        async def search(self, _entry):
            return [
                SimpleNamespace(path=path, entry_type="file")
                for path in sorted(self.files)
            ]

    class Commands:
        def __init__(self) -> None:
            self.runs = []
            self.interrupted: list[str] = []

        async def run(self, command, *, opts, handlers):
            self.runs.append((command, opts))
            if handlers.on_stdout:
                await handlers.on_stdout(SimpleNamespace(text="streamed output\n"))
            if handlers.on_stderr:
                await handlers.on_stderr(SimpleNamespace(text="streamed warning\n"))
            return SimpleNamespace(id=f"command-{len(self.runs)}", exit_code=0, error=None, logs=None)

        async def interrupt(self, execution_id: str) -> None:
            self.interrupted.append(execution_id)

    class Handle:
        def __init__(self) -> None:
            self.id = "server-sandbox-1"
            self.files = Files()
            self.commands = Commands()
            self.paused = False
            self.destroyed = False

        async def get_endpoint(self, port: int):
            assert port == 8080
            return SimpleNamespace(endpoint="preview.example.test:45678", headers={})

        async def pause(self) -> None:
            self.paused = True

        async def destroy(self) -> None:
            self.destroyed = True

    class SDK:
        handle = Handle()
        create_calls = []
        connected_ids: list[str] = []

        @classmethod
        async def create(cls, image, **kwargs):
            cls.create_calls.append((image, kwargs))
            return cls.handle

        @classmethod
        async def connect(cls, sandbox_id, **_kwargs):
            cls.connected_ids.append(sandbox_id)
            return cls.handle

    provider = OpenSandboxProvider(
        "http://sandbox.test",
        api_key="test-key",
        image="example/fomo-base:test",
        sandbox_class=SDK,
    )
    ref = await provider.create("project-1", SourceRef(version_id="version-1"))
    assert ref.id == "server-sandbox-1"
    image, create_kwargs = SDK.create_calls[0]
    assert image == "example/fomo-base:test"
    assert create_kwargs["platform"].arch == "arm64"
    assert create_kwargs["metadata"]["fomo.source_version_id"] == "version-1"
    assert create_kwargs["timeout"] == timedelta(seconds=21_600)
    assert "env" not in create_kwargs
    assert SDK.handle.files.directories == ["/workspace"]
    assert [entry.mode for entry in SDK.handle.files.directory_entries] == [755]

    await provider.apply_changes(
        ref,
        [FileChange(path="package.json", content='{"name":"first"}')],
    )
    await provider.apply_changes(
        ref,
        [FileChange(path="package.json", content='{"name":"second"}', operation="modify")],
    )
    package_writes = [
        entry for entry in SDK.handle.files.write_entries if entry.path == "/workspace/package.json"
    ]
    assert [entry.mode for entry in package_writes] == [644, 644]
    assert SDK.handle.files.delete_calls == []
    assert await provider.read_file(ref, "package.json") == b'{"name":"second"}'

    await provider.apply_changes(
        ref,
        [
            FileChange(path="src/app.ts", content="export const app = true"),
            FileChange(path="old.txt", operation="delete"),
        ],
    )
    assert await provider.read_file(ref, "src/app.ts") == b"export const app = true"
    assert "/workspace/old.txt" not in SDK.handle.files.files

    output: list[str] = []

    async def sink(_stream: str, text: str) -> None:
        output.append(text)

    result = await provider.exec(ref, Command("node --version", timeout_seconds=12), sink)
    assert result.exit_code == 0
    assert "streamed output" in result.stdout
    assert "streamed warning" in result.stderr
    assert "streamed output" in "".join(output)
    assert SDK.handle.commands.runs[0][1].working_directory == "/workspace"
    assert SDK.handle.commands.runs[0][1].timeout.total_seconds() == 12

    preview = await provider.start_preview(ref, Command("pnpm dev"), 8080, sink)
    assert preview.url == "http://preview.example.test:45678"
    assert SDK.handle.commands.runs[1][1].background is True
    assert SDK.handle.commands.runs[1][1].working_directory == "/workspace"
    manifest = await provider.list_files(ref)
    assert [item["path"] for item in manifest] == ["package.json", "src/app.ts"]
    await provider.pause(ref)
    assert SDK.handle.paused is True
    with pytest.raises(ValueError):
        await provider.expose(ref, 44772)
    with pytest.raises(NotImplementedError):
        await provider.snapshot(ref)

    reconnected = OpenSandboxProvider("http://sandbox.test", sandbox_class=SDK)
    assert await reconnected.connect(ref) is SDK.handle
    assert SDK.connected_ids == ["server-sandbox-1"]
    await reconnected.kill(ref)
    assert SDK.handle.destroyed is True


@pytest.mark.asyncio
async def test_opensandbox_proxy_environment_is_allowlisted_and_injected_only_when_explicit() -> None:
    class Files:
        async def create_directories(self, _entries) -> None:
            return None

    class Handle:
        id = "server-sandbox-proxy"
        files = Files()

        async def destroy(self) -> None:
            return None

    class SDK:
        create_kwargs = None

        @classmethod
        async def create(cls, _image, **kwargs):
            cls.create_kwargs = kwargs
            return Handle()

    provider = OpenSandboxProvider(
        "http://sandbox.test",
        sandbox_class=SDK,
        proxy_environment={
            "HTTP_PROXY": "http://host.docker.internal:7890",
            "NO_PROXY": "localhost,127.0.0.1",
        },
    )
    await provider.create("project-proxy")

    assert SDK.create_kwargs["env"] == {
        "HTTP_PROXY": "http://host.docker.internal:7890",
        "NO_PROXY": "localhost,127.0.0.1",
    }
    with pytest.raises(ValueError, match="only contain"):
        OpenSandboxProvider(
            "http://sandbox.test",
            sandbox_class=SDK,
            proxy_environment={"OPENAI_API_KEY": "must-not-cross-the-boundary"},
        )
    with pytest.raises(ValueError, match="without userinfo"):
        OpenSandboxProvider(
            "http://sandbox.test",
            sandbox_class=SDK,
            proxy_environment={"HTTP_PROXY": "http://user:password@proxy.example.test:7890"},
        )
