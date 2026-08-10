from __future__ import annotations

import platform as host_platform
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from opensandbox.exceptions import SandboxApiException
from pydantic import ValidationError

from fomo.config import DEFAULT_OPENSANDBOX_IMAGE, Settings
from fomo.runtime_contract import (
    RuntimeContractError,
    parse_enabled_profile_ids,
    resolve_runtime_contract,
    validated_default_profile_id,
)
from fomo.sandbox import create_sandbox_provider
from fomo.sandbox.base import Command, FileChange, SandboxPathError, SourceRef
from fomo.sandbox.fake import FakeSandboxProvider
from fomo.sandbox.opensandbox import OpenSandboxProvider, _OutputCollector
from fomo.sandbox.process import ProcessSandboxProvider
from fomo.schemas import MessageCreate, PreviewResponse, ProductSpec


def test_runtime_selection_uses_the_selected_profile_default_thinking() -> None:
    payload = MessageCreate.model_validate(
        {
            "clientMessageId": "kimi-default",
            "content": "Build a polished product",
            "profileId": "kimi-k2.7-code",
        }
    )

    assert payload.profile_id == "kimi-k2.7-code"
    assert payload.thinking is None


def test_runtime_allowlist_has_unlimited_cumulative_tokens_and_bounded_throughput() -> None:
    enabled = parse_enabled_profile_ids("gpt-5.6")
    assert enabled == frozenset({"gpt-5.6"})
    with pytest.raises(RuntimeContractError, match="must be enabled"):
        validated_default_profile_id("deepseek-flash", enabled)

    runtime = resolve_runtime_contract(
        "gpt-5.6",
        "xhigh",
        inference_tpm_limit=300_001,
        max_spend_micros=500_000,
    )
    assert runtime.run_max_tokens is None
    assert runtime.inference_tpm_limit == 300_001
    assert runtime.max_spend_micros == 500_000


def test_opensandbox_exit_code_minus_one_is_a_timeout_without_sdk_error() -> None:
    assert OpenSandboxProvider._execution_timed_out(None, exit_code=-1)


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


def test_preview_ready_requires_run_id_and_absolute_http_url() -> None:
    PreviewResponse.model_validate(
        {"status": "ready", "url": "https://preview.example.test/app", "runId": "run-1", "verificationStatus": "verified"}
    )
    PreviewResponse.model_validate(
        {"status": "ready", "url": "http://localhost:3000/app", "runId": "run-1", "verificationStatus": "unverified"}
    )
    PreviewResponse.model_validate(
        {"status": "ready", "url": "https://preview.example.test:8443/app?x=1", "runId": "run-1", "verificationStatus": "verified"}
    )

    with pytest.raises(ValidationError, match="verificationStatus"):
        PreviewResponse.model_validate(
            {"status": "ready", "url": "https://preview.example.test/app", "runId": "run-1"}
        )

    with pytest.raises(ValidationError, match="runId"):
        PreviewResponse.model_validate({"status": "ready", "url": "https://preview.example.test/app"})
    with pytest.raises(ValidationError, match="url"):
        PreviewResponse.model_validate({"status": "ready", "url": None, "runId": "run-1"})
    with pytest.raises(ValidationError, match="absolute"):
        PreviewResponse.model_validate({"status": "ready", "url": "/relative/path", "runId": "run-1"})
    with pytest.raises(ValidationError, match="absolute"):
        PreviewResponse.model_validate({"status": "ready", "url": "app/page", "runId": "run-1"})
    with pytest.raises(ValidationError, match="absolute"):
        PreviewResponse.model_validate({"status": "ready", "url": "javascript:alert(1)", "runId": "run-1"})
    with pytest.raises(ValidationError, match="absolute"):
        PreviewResponse.model_validate({"status": "ready", "url": "ftp://preview.example.test/app", "runId": "run-1"})
    with pytest.raises(ValidationError, match="userinfo"):
        PreviewResponse.model_validate(
            {"status": "ready", "url": "https://user:pass@preview.example.test/app", "runId": "run-1"}
        )


def test_preview_non_ready_status_requires_null_url() -> None:
    PreviewResponse.model_validate({"status": "expired", "url": None, "runId": "run-1"})
    PreviewResponse.model_validate({"status": "unavailable", "url": None, "runId": None})

    with pytest.raises(ValidationError, match="url null"):
        PreviewResponse.model_validate(
            {"status": "expired", "url": "https://preview.example.test/app", "runId": "run-1"}
        )
    with pytest.raises(ValidationError, match="url null"):
        PreviewResponse.model_validate(
            {"status": "unavailable", "url": "https://preview.example.test/app", "runId": None}
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


def test_verified_preview_lifetime_defaults_to_seven_days_and_is_bounded(monkeypatch) -> None:
    monkeypatch.delenv("VERIFIED_PREVIEW_LIFETIME_SECONDS", raising=False)
    assert Settings.from_env().verified_preview_lifetime_seconds == 604_800

    monkeypatch.setenv("VERIFIED_PREVIEW_LIFETIME_SECONDS", "86400")
    assert Settings.from_env().verified_preview_lifetime_seconds == 86_400

    for value in ("0", "-1", "604801"):
        monkeypatch.setenv("VERIFIED_PREVIEW_LIFETIME_SECONDS", value)
        with pytest.raises(ValueError, match="VERIFIED_PREVIEW_LIFETIME_SECONDS"):
            Settings.from_env()

    with pytest.raises(ValueError, match="must not exceed 604800"):
        Settings(verified_preview_lifetime_seconds=604_801)


def test_opensandbox_lifetime_is_forwarded_by_factory(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class CapturingOpenSandboxProvider:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("fomo.sandbox.OpenSandboxProvider", CapturingOpenSandboxProvider)

    create_sandbox_provider(
        Settings(opensandbox_lifetime_seconds=1234, opensandbox_ready_timeout_seconds=90)
    )

    assert captured["lifetime_seconds"] == 1234
    assert captured["ready_timeout_seconds"] == 90


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
async def test_opensandbox_output_collector_joins_messages_like_sdk_and_keeps_truncation_streaming() -> None:
    emitted: list[tuple[str, str]] = []

    async def sink(stream: str, text: str) -> None:
        emitted.append((stream, text))

    collector = _OutputCollector(sink, limit_bytes=128)
    await collector.emit("stdout", SimpleNamespace(text="first line\n"))
    await collector.emit("stdout", SimpleNamespace(text="second line"))
    await collector.emit("stderr", SimpleNamespace(text="warning\n"))
    await collector.emit("stderr", SimpleNamespace(text="detail"))

    assert collector.stdout == "first line\nsecond line"
    assert collector.stderr == "warning\ndetail"
    assert emitted == [
        ("stdout", "first line\n"),
        ("stdout", "second line"),
        ("stderr", "warning\n"),
        ("stderr", "detail"),
    ]

    truncated = _OutputCollector(sink, limit_bytes=5)
    await truncated.emit("stdout", SimpleNamespace(text="ab\n"))
    await truncated.emit("stdout", SimpleNamespace(text="cde"))
    await truncated.finish()

    assert truncated.stdout == "ab\ncd"
    assert truncated.stderr == "\n[output truncated]"
    assert emitted[-3:] == [
        ("stdout", "ab\n"),
        ("stdout", "cd"),
        ("stderr", "\n[output truncated]\n"),
    ]


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
            self.renewals: list[timedelta] = []

        async def get_endpoint(self, port: int):
            assert port == 8080
            return SimpleNamespace(endpoint="preview.example.test:45678", headers={})

        async def get_info(self):
            state = "Paused" if self.paused else "Running"
            return SimpleNamespace(status=SimpleNamespace(state=state))

        async def renew(self, timeout: timedelta):
            self.renewals.append(timeout)
            return SimpleNamespace(expires_at=datetime(2026, 8, 17, 1, 2, 3, tzinfo=UTC))

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
    expected_arch = "arm64" if host_platform.machine().lower() in {"arm64", "aarch64"} else "amd64"
    assert create_kwargs["platform"].arch == expected_arch
    assert create_kwargs["metadata"]["fomo.source_version_id"] == "version-1"
    assert create_kwargs["timeout"] == timedelta(seconds=21_600)
    assert create_kwargs["ready_timeout"] == timedelta(seconds=120)
    assert create_kwargs["connection_config"].request_timeout == timedelta(seconds=120)
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

    starter_copy = await provider.copy_starter(ref, "fomo-next-radix-v2")
    assert starter_copy.exit_code == 0
    assert SDK.handle.commands.runs[1][0] == (
        "cp -R --no-preserve=mode,ownership -- "
        "/opt/fomo/starters/fomo-next-radix-v2/base/. /workspace/ && "
        "test -L /workspace/node_modules && "
        "rm -- /workspace/node_modules && "
        "cp -a --no-preserve=ownership -- "
        "/opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules "
        "/workspace/node_modules && "
        "chmod -R u+rwX -- /workspace/node_modules"
    )
    assert SDK.handle.commands.runs[1][1].timeout == timedelta(seconds=120)
    with pytest.raises(ValueError, match="unsupported immutable starter"):
        await provider.copy_starter(ref, "unapproved-starter")

    preview = await provider.start_preview(ref, Command("pnpm dev"), 8080, sink)
    assert preview.url == "http://preview.example.test:45678"
    assert SDK.handle.commands.runs[2][1].background is True
    assert SDK.handle.commands.runs[2][1].working_directory == "/workspace"
    assert await provider.probe_preview(ref) is True
    assert await provider.renew_preview(ref, 604_800) == "2026-08-17T01:02:03+00:00"
    assert SDK.handle.renewals == [timedelta(seconds=604_800)]
    manifest = await provider.list_files(ref)
    assert [item["path"] for item in manifest] == ["package.json", "src/app.ts"]
    await provider.pause(ref)
    assert SDK.handle.paused is True
    assert await provider.probe_preview(ref) is False
    with pytest.raises(ValueError):
        await provider.expose(ref, 44772)
    with pytest.raises(NotImplementedError):
        await provider.snapshot(ref)

    temporary_ref = await provider.create(
        "runtime-preflight",
        lifetime_seconds=300,
    )
    assert SDK.create_calls[-1][1]["timeout"] == timedelta(seconds=300)
    with pytest.raises(ValueError, match="temporary OpenSandbox lifetime"):
        await provider.create("runtime-preflight-invalid", lifetime_seconds=21_601)
    await provider.kill(temporary_ref)

    reconnected = OpenSandboxProvider("http://sandbox.test", sandbox_class=SDK)
    assert await reconnected.connect(ref) is SDK.handle
    assert SDK.connected_ids == ["server-sandbox-1"]
    await reconnected.kill(ref)
    assert SDK.handle.destroyed is True


@pytest.mark.asyncio
async def test_opensandbox_preview_probe_evicts_only_confirmed_missing_resource() -> None:
    class Files:
        async def create_directories(self, _entries) -> None:
            return None

    class Handle:
        id = "server-sandbox-expired"
        files = Files()

        def __init__(self) -> None:
            self.status_code = 404
            self.closed = False

        async def get_info(self):
            raise SandboxApiException("probe failed", status_code=self.status_code)

        async def close(self) -> None:
            self.closed = True

        async def destroy(self) -> None:
            return None

    class SDK:
        handle = Handle()

        @classmethod
        async def create(cls, _image, **_kwargs):
            return cls.handle

    provider = OpenSandboxProvider("http://sandbox.test", sandbox_class=SDK)
    ref = await provider.create("project-expired")

    assert await provider.probe_preview(ref) is False
    assert SDK.handle.closed is True
    assert ref.id not in provider._sandboxes

    SDK.handle = Handle()
    SDK.handle.status_code = 503
    ref = await provider.create("project-transient")
    with pytest.raises(SandboxApiException):
        await provider.probe_preview(ref)
    assert SDK.handle.closed is False
    assert ref.id in provider._sandboxes


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
