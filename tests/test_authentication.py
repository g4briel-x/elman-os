import base64
import hashlib
import hmac
import json
import unittest
from dataclasses import replace

from elman_os.audit import AuthenticationMethod
from elman_os.authentication import (
    AuthenticationErrorCode,
    HmacSha256Verifier,
    JwtOidcAuthenticator,
    TokenAuthenticationError,
    TokenValidationPolicy,
)

KEY = b"jwt-test-key-with-at-least-32-bytes"
NOW = 2_000_000_000.0


def encode(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def token(
    claims: dict[str, object] | None = None,
    *,
    header: dict[str, object] | None = None,
    key: bytes = KEY,
) -> str:
    actual_header = header or {"alg": "HS256", "typ": "JWT", "kid": "active"}
    actual_claims = claims or {
        "iss": "https://identity.example",
        "aud": "elman-api",
        "sub": "user-123",
        "tenant_id": "tenant-456",
        "roles": ["ai.execute", "ai.read"],
        "iat": NOW - 10,
        "exp": NOW + 300,
    }
    signing_input = f"{encode(actual_header)}.{encode(actual_claims)}"
    signature = hmac.new(key, signing_input.encode(), hashlib.sha256).digest()
    return (
        f"{signing_input}."
        f"{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    )


def policy(**changes: object) -> TokenValidationPolicy:
    base = TokenValidationPolicy(
        issuer="https://identity.example",
        audiences=frozenset({"elman-api"}),
        algorithms=frozenset({"HS256"}),
        required_roles=frozenset({"ai.execute"}),
    )
    return replace(base, **changes)


def authenticator(**changes: object) -> JwtOidcAuthenticator:
    return JwtOidcAuthenticator(
        policy(**changes),
        HmacSha256Verifier({"active": KEY}),
        clock=lambda: NOW,
    )


def assert_code(
    case: unittest.TestCase,
    expected: AuthenticationErrorCode,
    action,
) -> None:
    with case.assertRaises(TokenAuthenticationError) as raised:
        action()
    case.assertEqual(raised.exception.code, expected)
    case.assertNotIn("eyJ", str(raised.exception))


class JwtOidcSuccessTests(unittest.TestCase):
    def test_valid_token_creates_execution_principal(self) -> None:
        principal = authenticator().authenticate(token())
        self.assertEqual(principal.subject_id, "user-123")
        self.assertEqual(principal.tenant_id, "tenant-456")
        self.assertEqual(
            principal.authentication_method, AuthenticationMethod.OIDC
        )
        self.assertEqual(
            principal.roles, frozenset({"ai.execute", "ai.read"})
        )

    def test_jwt_method_and_space_delimited_roles_are_supported(self) -> None:
        claims = {
            "iss": "https://identity.example",
            "aud": "elman-api",
            "sub": "service-1",
            "tenant_id": "tenant-1",
            "roles": "ai.execute ai.read",
            "iat": NOW,
            "exp": NOW + 60,
        }
        principal = authenticator(
            authentication_method=AuthenticationMethod.JWT
        ).authenticate(token(claims))
        self.assertEqual(principal.authentication_method, AuthenticationMethod.JWT)

    def test_nonce_and_multiple_oidc_audiences_are_validated(self) -> None:
        claims = {
            "iss": "https://identity.example",
            "aud": ["elman-api", "other-resource"],
            "azp": "elman-api",
            "sub": "user-123",
            "tenant_id": "tenant-456",
            "roles": ["ai.execute"],
            "nonce": "nonce-123",
            "iat": NOW,
            "exp": NOW + 60,
        }
        principal = authenticator(required_nonce="nonce-123").authenticate(
            token(claims)
        )
        self.assertEqual(principal.subject_id, "user-123")


class JwtOidcSignatureTests(unittest.TestCase):
    def test_tampered_payload_is_rejected(self) -> None:
        valid = token()
        header, _, signature = valid.split(".")
        tampered = {
            "iss": "https://identity.example",
            "aud": "elman-api",
            "sub": "attacker",
            "tenant_id": "tenant-456",
            "roles": ["ai.execute"],
            "iat": NOW,
            "exp": NOW + 300,
        }
        assert_code(
            self,
            AuthenticationErrorCode.SIGNATURE_INVALID,
            lambda: authenticator().authenticate(
                f"{header}.{encode(tampered)}.{signature}"
            ),
        )

    def test_alg_none_is_rejected_before_verifier(self) -> None:
        unsigned = f"{encode({'alg': 'none'})}.{encode({'sub': 'x'})}.AA"
        assert_code(
            self,
            AuthenticationErrorCode.UNSUPPORTED_ALGORITHM,
            lambda: authenticator().authenticate(unsigned),
        )

    def test_unknown_key_id_is_rejected(self) -> None:
        assert_code(
            self,
            AuthenticationErrorCode.SIGNATURE_INVALID,
            lambda: authenticator().authenticate(
                token(header={"alg": "HS256", "kid": "unknown"})
            ),
        )

    def test_short_hmac_key_is_rejected_and_repr_redacts_keys(self) -> None:
        with self.assertRaises(ValueError):
            HmacSha256Verifier({"bad": b"short"})
        verifier = HmacSha256Verifier({"active": KEY})
        self.assertNotIn(KEY.decode(), repr(verifier))
        self.assertIn("redacted", repr(verifier))


class JwtOidcClaimsTests(unittest.TestCase):
    def base_claims(self) -> dict[str, object]:
        return {
            "iss": "https://identity.example",
            "aud": "elman-api",
            "sub": "user-123",
            "tenant_id": "tenant-456",
            "roles": ["ai.execute"],
            "iat": NOW,
            "exp": NOW + 60,
        }

    def test_expired_token_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["exp"] = NOW - 31
        assert_code(
            self,
            AuthenticationErrorCode.TOKEN_EXPIRED,
            lambda: authenticator().authenticate(token(claims)),
        )

    def test_future_nbf_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["nbf"] = NOW + 31
        assert_code(
            self,
            AuthenticationErrorCode.TOKEN_NOT_YET_VALID,
            lambda: authenticator().authenticate(token(claims)),
        )

    def test_token_issued_too_far_in_future_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["iat"] = NOW + 31
        assert_code(
            self,
            AuthenticationErrorCode.TOKEN_ISSUED_IN_FUTURE,
            lambda: authenticator().authenticate(token(claims)),
        )

    def test_old_token_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["iat"] = NOW - 3631
        assert_code(
            self,
            AuthenticationErrorCode.TOKEN_TOO_OLD,
            lambda: authenticator().authenticate(token(claims)),
        )

    def test_issuer_mismatch_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["iss"] = "https://attacker.example"
        assert_code(
            self,
            AuthenticationErrorCode.ISSUER_MISMATCH,
            lambda: authenticator().authenticate(token(claims)),
        )

    def test_audience_mismatch_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["aud"] = "other-api"
        assert_code(
            self,
            AuthenticationErrorCode.AUDIENCE_MISMATCH,
            lambda: authenticator().authenticate(token(claims)),
        )

    def test_missing_required_role_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["roles"] = ["ai.read"]
        assert_code(
            self,
            AuthenticationErrorCode.ROLES_INVALID,
            lambda: authenticator().authenticate(token(claims)),
        )

    def test_missing_tenant_is_rejected(self) -> None:
        claims = self.base_claims()
        del claims["tenant_id"]
        assert_code(
            self,
            AuthenticationErrorCode.REQUIRED_CLAIM_MISSING,
            lambda: authenticator().authenticate(token(claims)),
        )

    def test_nonce_mismatch_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["nonce"] = "wrong"
        assert_code(
            self,
            AuthenticationErrorCode.NONCE_MISMATCH,
            lambda: authenticator(required_nonce="expected").authenticate(
                token(claims)
            ),
        )

    def test_boolean_timestamp_is_rejected(self) -> None:
        claims = self.base_claims()
        claims["exp"] = True
        assert_code(
            self,
            AuthenticationErrorCode.CLAIM_TYPE_INVALID,
            lambda: authenticator().authenticate(token(claims)),
        )


class JwtOidcParsingTests(unittest.TestCase):
    def test_wrong_segment_count_is_rejected(self) -> None:
        assert_code(
            self,
            AuthenticationErrorCode.MALFORMED_TOKEN,
            lambda: authenticator().authenticate("one.two"),
        )

    def test_padded_compact_segment_is_rejected(self) -> None:
        assert_code(
            self,
            AuthenticationErrorCode.MALFORMED_TOKEN,
            lambda: authenticator().authenticate("e30=.e30.AA"),
        )

    def test_duplicate_header_member_is_rejected(self) -> None:
        header = base64.urlsafe_b64encode(
            b'{"alg":"HS256","alg":"none","kid":"active"}'
        ).rstrip(b"=").decode()
        claims = encode({"sub": "x"})
        assert_code(
            self,
            AuthenticationErrorCode.INVALID_HEADER,
            lambda: authenticator().authenticate(f"{header}.{claims}.AA"),
        )


if __name__ == "__main__":
    unittest.main()
