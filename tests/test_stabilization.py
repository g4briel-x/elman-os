import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from elman_os.audit import (
    AuditEventType,
    AuditIntegrityError,
    AuditSigner,
    AuditTrail,
    AuditedAIExecutor,
    AuthenticationMethod,
    ExecutionAuditContext,
    ExecutionPrincipal,
    ExecutionPurpose,
    FileAuditSink,
    InMemoryAuditSink,
)
from elman_os.configuration import ProviderSettings, SecretValue
from elman_os.execution import ResilientAIExecutor, RetryPolicy
from elman_os.governance import (
    CompatibilityErrorCode,
    ConfigurationCompatibilityError,
    IdentityQuota,
    IdentityQuotaErrorCode,
    IdentityQuotaExceededError,
    IdentityQuotaManager,
    StabilizedAIRuntime,
    check_configuration_compatibility,
)
from elman_os.provider import (
    DeterministicModelProvider,
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
)
from elman_os.registry import built_in_provider_registry

KEY = b"alpha-seven-audit-key-with-32-bytes-minimum"


def settings() -> ProviderSettings:
    return ProviderSettings(
        provider_id="deterministic-model",
        model="deterministic-v1",
        auth_mode="none",
    )


def principal(subject: str = "person-1") -> ExecutionPrincipal:
    return ExecutionPrincipal(
        subject_id=subject,
        tenant_id="tenant-1",
        authentication_method=AuthenticationMethod.LOCAL_TEST,
        roles=frozenset({"ai.execute"}),
    )


def context(subject: str = "person-1") -> ExecutionAuditContext:
    return ExecutionAuditContext(
        principal=principal(subject),
        purpose=ExecutionPurpose.EVALUATION,
        correlation_id=f"corr-{subject}",
    )


def request(identifier: str = "req-1") -> ModelRequest:
    return ModelRequest(
        request_id=identifier,
        model="deterministic-v1",
        messages=(ModelMessage(MessageRole.USER, "Validation hors réseau"),),
        max_output_tokens=20,
    )


class ConfigurationCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = built_in_provider_registry()

    def test_deterministic_configuration_is_compatible(self) -> None:
        report = check_configuration_compatibility(settings(), self.registry)
        self.assertTrue(report.valid)
        self.assertEqual(report.error_codes, ())

    def test_unknown_provider_is_rejected_without_factory_call(self) -> None:
        candidate = replace(
            settings(),
            provider_id="unknown",
            model="unknown-v1",
            auth_mode="api_key",
            api_key=SecretValue("fake"),
        )
        report = check_configuration_compatibility(candidate, self.registry)
        self.assertIn(
            CompatibilityErrorCode.PROVIDER_NOT_REGISTERED, report.error_codes
        )

    def test_undeclared_model_is_rejected(self) -> None:
        report = check_configuration_compatibility(
            replace(settings(), model="deterministic-v2"),
            self.registry,
        )
        self.assertIn(CompatibilityErrorCode.MODEL_NOT_DECLARED, report.error_codes)

    def test_missing_capability_is_rejected(self) -> None:
        report = check_configuration_compatibility(
            settings(),
            self.registry,
            required_capabilities=frozenset(
                {ModelCapability.TEXT_GENERATION, ModelCapability.VISION}
            ),
        )
        self.assertIn(
            CompatibilityErrorCode.CAPABILITY_NOT_DECLARED, report.error_codes
        )

    def test_openai_compatible_requires_base_url(self) -> None:
        candidate = ProviderSettings(
            provider_id="openai-compatible",
            model="compatible-model",
            auth_mode="api_key",
            api_key=SecretValue("fake"),
        )
        report = check_configuration_compatibility(candidate, self.registry)
        self.assertIn(CompatibilityErrorCode.BASE_URL_REQUIRED, report.error_codes)

    def test_safe_summary_never_contains_key(self) -> None:
        candidate = ProviderSettings(
            provider_id="openai",
            model="gpt-test",
            auth_mode="api_key",
            api_key=SecretValue("never-print-this"),
        )
        summary = check_configuration_compatibility(
            candidate, self.registry
        ).safe_summary()
        self.assertNotIn("never-print-this", json.dumps(summary))


class IdentityQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_reservation_updates_atomic_snapshot(self) -> None:
        manager = IdentityQuotaManager(IdentityQuota(3, 100, 2))
        reservation = await manager.reserve("fingerprint", 25)
        snapshot = await manager.snapshot("fingerprint")
        self.assertEqual((snapshot.requests, snapshot.active), (1, 1))
        self.assertEqual(snapshot.reserved_tokens, 25)
        await manager.settle(reservation, actual_tokens=10)

    async def test_request_limit_is_enforced(self) -> None:
        manager = IdentityQuotaManager(IdentityQuota(1, 100, 1))
        first = await manager.reserve("fingerprint", 10)
        await manager.settle(first, actual_tokens=5)
        with self.assertRaises(IdentityQuotaExceededError) as raised:
            await manager.reserve("fingerprint", 10)
        self.assertEqual(raised.exception.code, IdentityQuotaErrorCode.REQUEST_LIMIT)

    async def test_token_limit_counts_reserved_capacity(self) -> None:
        manager = IdentityQuotaManager(IdentityQuota(3, 20, 2))
        await manager.reserve("fingerprint", 15)
        with self.assertRaises(IdentityQuotaExceededError) as raised:
            await manager.reserve("fingerprint", 10)
        self.assertEqual(raised.exception.code, IdentityQuotaErrorCode.TOKEN_LIMIT)

    async def test_concurrency_limit_is_enforced(self) -> None:
        manager = IdentityQuotaManager(IdentityQuota(3, 100, 1))
        await manager.reserve("fingerprint", 10)
        with self.assertRaises(IdentityQuotaExceededError) as raised:
            await manager.reserve("fingerprint", 10)
        self.assertEqual(
            raised.exception.code, IdentityQuotaErrorCode.CONCURRENCY_LIMIT
        )

    async def test_settlement_releases_concurrency(self) -> None:
        manager = IdentityQuotaManager(IdentityQuota(3, 100, 1))
        first = await manager.reserve("fingerprint", 10)
        await manager.settle(first, actual_tokens=4)
        second = await manager.reserve("fingerprint", 10)
        self.assertIsNotNone(second)
        await manager.settle(second, actual_tokens=3)
        with self.assertRaises(ValueError):
            await manager.settle(second, actual_tokens=3)

    async def test_identities_have_independent_quotas(self) -> None:
        manager = IdentityQuotaManager(IdentityQuota(1, 100, 1))
        await manager.reserve("identity-a", 10)
        reservation = await manager.reserve("identity-b", 10)
        self.assertEqual(reservation.identity_fingerprint, "identity-b")

    async def test_invalid_estimate_is_rejected(self) -> None:
        manager = IdentityQuotaManager()
        with self.assertRaises(ValueError):
            await manager.reserve("fingerprint", 0)

    async def test_actual_tokens_are_accounted_after_settlement(self) -> None:
        manager = IdentityQuotaManager(IdentityQuota(3, 100, 1))
        reservation = await manager.reserve("fingerprint", 50)
        await manager.settle(reservation, actual_tokens=7)
        snapshot = await manager.snapshot("fingerprint")
        self.assertEqual((snapshot.tokens, snapshot.active), (7, 0))
        self.assertEqual(snapshot.reserved_tokens, 0)


class PersistentAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "audit" / "events.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_executor(self, sink: FileAuditSink) -> AuditedAIExecutor:
        return AuditedAIExecutor(
            ResilientAIExecutor(
                DeterministicModelProvider(),
                RetryPolicy(max_attempts=1),
            ),
            AuditTrail.resume(AuditSigner(KEY), sink),
        )

    async def test_events_are_durably_reloaded(self) -> None:
        sink = FileAuditSink(self.path)
        executor = self.make_executor(sink)
        await executor.generate(request(), context())
        self.assertEqual(len(sink.load()), 2)

    async def test_resumed_trail_continues_signature_chain(self) -> None:
        sink = FileAuditSink(self.path)
        await self.make_executor(sink).generate(request("req-1"), context())
        resumed = self.make_executor(sink)
        await resumed.generate(request("req-2"), context())
        events = sink.load()
        self.assertEqual(len(events), 4)
        self.assertTrue(resumed.trail.verify_chain(events))

    async def test_tampering_is_rejected_during_resume(self) -> None:
        sink = FileAuditSink(self.path)
        await self.make_executor(sink).generate(request(), context())
        text = self.path.read_text(encoding="utf-8").replace(
            '"output_tokens":3', '"output_tokens":999'
        )
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(AuditIntegrityError):
            AuditTrail.resume(AuditSigner(KEY), sink)

    async def test_malformed_json_is_rejected(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not-json}\n", encoding="utf-8")
        with self.assertRaises(AuditIntegrityError):
            FileAuditSink(self.path).load()

    async def test_maximum_file_size_is_fail_closed(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("x" * 20, encoding="utf-8")
        with self.assertRaises(AuditIntegrityError):
            FileAuditSink(self.path, max_file_bytes=10).load()


class StabilizedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_runtime_executes_and_audits(self) -> None:
        sink = InMemoryAuditSink()
        runtime = StabilizedAIRuntime.from_settings(
            built_in_provider_registry(),
            settings(),
            signer=AuditSigner(KEY),
            sink=sink,
        )
        try:
            result = await runtime.executor.generate(request(), context())
        finally:
            await runtime.close()
        self.assertEqual(result.response.provider_id, "deterministic-model")
        self.assertEqual(
            [item.event.event_type for item in sink.events],
            [AuditEventType.STARTED, AuditEventType.SUCCEEDED],
        )

    async def test_incompatible_configuration_fails_before_runtime(self) -> None:
        with self.assertRaises(ConfigurationCompatibilityError):
            StabilizedAIRuntime.from_settings(
                built_in_provider_registry(),
                replace(settings(), model="invalid"),
                signer=AuditSigner(KEY),
                sink=InMemoryAuditSink(),
            )

    async def test_quota_denial_is_audited_before_provider(self) -> None:
        sink = InMemoryAuditSink()
        quotas = IdentityQuotaManager(IdentityQuota(1, 10_000, 1))
        runtime = StabilizedAIRuntime.from_settings(
            built_in_provider_registry(),
            settings(),
            signer=AuditSigner(KEY),
            sink=sink,
            quotas=quotas,
        )
        try:
            await runtime.executor.generate(request("req-1"), context())
            with self.assertRaises(IdentityQuotaExceededError):
                await runtime.executor.generate(request("req-2"), context())
        finally:
            await runtime.close()
        self.assertEqual(sink.events[-1].event.event_type, AuditEventType.DENIED)
        self.assertEqual(
            sink.events[-1].event.error_code, "identity_request_limit"
        )

    async def test_separate_identities_receive_separate_allowances(self) -> None:
        runtime = StabilizedAIRuntime.from_settings(
            built_in_provider_registry(),
            settings(),
            signer=AuditSigner(KEY),
            sink=InMemoryAuditSink(),
            quotas=IdentityQuotaManager(IdentityQuota(1, 10_000, 1)),
        )
        try:
            first = await runtime.executor.generate(request("a"), context("a"))
            second = await runtime.executor.generate(request("b"), context("b"))
        finally:
            await runtime.close()
        self.assertEqual(first.response.content, second.response.content)

    async def test_cancellation_releases_identity_concurrency(self) -> None:
        entered = asyncio.Event()

        class BlockingProvider(DeterministicModelProvider):
            async def generate(self, model_request: ModelRequest):
                entered.set()
                await asyncio.Event().wait()

        sink = InMemoryAuditSink()
        quotas = IdentityQuotaManager(IdentityQuota(2, 10_000, 1))
        audited = AuditedAIExecutor(
            ResilientAIExecutor(BlockingProvider()),
            AuditTrail(AuditSigner(KEY), sink),
        )
        from elman_os.governance import StabilizedAIExecutor

        executor = StabilizedAIExecutor(audited, quotas, "deterministic-v1")
        task = asyncio.create_task(executor.generate(request(), context()))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        identity = AuditSigner(KEY).fingerprint("quota", "tenant-1\0person-1")
        self.assertEqual((await quotas.snapshot(identity)).active, 0)
        self.assertEqual(sink.events[-1].event.event_type, AuditEventType.CANCELLED)


if __name__ == "__main__":
    unittest.main()
