import tempfile
import unittest
from pathlib import Path

from elman_os.audit import (
    AuditEventType,
    AuditSigner,
    AuthenticationMethod,
    ExecutionAuditContext,
    ExecutionAuthorizationError,
    ExecutionPrincipal,
    ExecutionPurpose,
)
from elman_os.execution import ResilientAIExecutor, RetryPolicy
from elman_os.governance import IdentityQuota, IdentityQuotaExceededError
from elman_os.production_runtime import PersistentGovernedAIExecutor
from elman_os.provider import (
    DeterministicModelProvider,
    MessageRole,
    ModelMessage,
    ModelRequest,
)
from elman_os.transactional_persistence import SQLitePersistence


KEY = b"production-runtime-test-key-32-bytes-minimum"


def principal(
    tenant: str = "tenant-a",
    roles: frozenset[str] = frozenset({"ai.execute"}),
) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        subject_id="subject-1",
        tenant_id=tenant,
        authentication_method=AuthenticationMethod.JWT,
        roles=roles,
    )


def context(
    tenant: str = "tenant-a",
    roles: frozenset[str] = frozenset({"ai.execute"}),
) -> ExecutionAuditContext:
    return ExecutionAuditContext(
        principal(tenant, roles),
        ExecutionPurpose.AGENT_TASK,
        f"corr-{tenant}",
    )


def request(identifier: str = "request-1") -> ModelRequest:
    return ModelRequest(
        request_id=identifier,
        model="client-selected-model-is-overridden",
        messages=(ModelMessage(MessageRole.USER, "Bonjour ELMAN"),),
        max_output_tokens=20,
    )


class ProductionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "production.sqlite3"
        self.backend = SQLitePersistence(self.path)
        self.provider = DeterministicModelProvider(responses=("Réponse sûre",))
        self.executor = PersistentGovernedAIExecutor(
            ResilientAIExecutor(
                self.provider,
                retry_policy=RetryPolicy(max_attempts=1),
            ),
            self.backend,
            AuditSigner(KEY),
            "deterministic-v1",
            quota=IdentityQuota(
                max_requests=10,
                max_tokens=10_000,
                max_concurrent=2,
            ),
        )

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self.temp.cleanup()

    async def test_execution_uses_persistent_quota_and_audit(self) -> None:
        result = await self.executor.generate(request(), context())
        self.assertEqual(result.response.content, "Réponse sûre")
        trail = self.executor.audit_trail("tenant-a")
        events = await trail.load()
        self.assertEqual(
            [item.event.event_type for item in events],
            [AuditEventType.STARTED, AuditEventType.SUCCEEDED],
        )
        self.assertTrue(await trail.verify_persisted())

    async def test_selected_model_overrides_untrusted_request_model(self) -> None:
        result = await self.executor.generate(request(), context())
        self.assertEqual(result.response.model, "deterministic-v1")
        self.assertEqual(self.provider.requests[0].model, "deterministic-v1")

    async def test_missing_role_is_denied_before_provider_call(self) -> None:
        with self.assertRaises(ExecutionAuthorizationError):
            await self.executor.generate(
                request(),
                context(roles=frozenset({"ai.read"})),
            )
        self.assertEqual(self.executor.executor.ledger.provider_calls, 0)
        events = await self.executor.audit_trail("tenant-a").load()
        self.assertEqual(events[-1].event.event_type, AuditEventType.DENIED)

    async def test_tenant_audit_chains_are_isolated(self) -> None:
        await self.executor.generate(request("request-a"), context("tenant-a"))
        await self.executor.generate(request("request-b"), context("tenant-b"))
        first = await self.executor.audit_trail("tenant-a").load()
        second = await self.executor.audit_trail("tenant-b").load()
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertNotEqual(
            first[0].event.tenant_fingerprint,
            second[0].event.tenant_fingerprint,
        )

    async def test_quota_is_shared_by_two_runtime_instances(self) -> None:
        quota = IdentityQuota(max_requests=1, max_tokens=10_000, max_concurrent=2)
        first = PersistentGovernedAIExecutor(
            ResilientAIExecutor(
                DeterministicModelProvider(responses=("Un",)),
                retry_policy=RetryPolicy(max_attempts=1),
            ),
            self.backend,
            AuditSigner(KEY),
            "deterministic-v1",
            quota=quota,
        )
        second = PersistentGovernedAIExecutor(
            ResilientAIExecutor(
                DeterministicModelProvider(responses=("Deux",)),
                retry_policy=RetryPolicy(max_attempts=1),
            ),
            self.backend,
            AuditSigner(KEY),
            "deterministic-v1",
            quota=quota,
        )
        await first.generate(request("request-a"), context())
        with self.assertRaises(IdentityQuotaExceededError):
            await second.generate(request("request-b"), context())

    async def test_audit_survives_backend_reopen(self) -> None:
        await self.executor.generate(request(), context())
        await self.backend.close()
        reopened = SQLitePersistence(self.path)
        try:
            replacement = PersistentGovernedAIExecutor(
                ResilientAIExecutor(
                    DeterministicModelProvider(),
                    retry_policy=RetryPolicy(max_attempts=1),
                ),
                reopened,
                AuditSigner(KEY),
                "deterministic-v1",
            )
            self.assertTrue(
                await replacement.audit_trail("tenant-a").verify_persisted()
            )
        finally:
            await reopened.close()

    async def test_persistent_trail_exposes_audited_executor_clock_contract(self) -> None:
        trail = self.executor.audit_trail("tenant-a")
        self.assertTrue(callable(trail.event_id_factory))
        self.assertTrue(callable(trail.wall_clock))


if __name__ == "__main__":
    unittest.main()
