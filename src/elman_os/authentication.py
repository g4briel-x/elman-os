"""Offline JWT/OIDC authentication boundary for ELMAN-OS."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .audit import AuthenticationMethod, ExecutionPrincipal

_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_TOKEN_BYTES = 16_384
_MAX_SEGMENT_BYTES = 12_288


class AuthenticationErrorCode(StrEnum):
    """Stable, non-sensitive authentication failure codes."""

    MALFORMED_TOKEN = "malformed_token"
    INVALID_HEADER = "invalid_header"
    UNSUPPORTED_ALGORITHM = "unsupported_algorithm"
    SIGNATURE_INVALID = "signature_invalid"
    REQUIRED_CLAIM_MISSING = "required_claim_missing"
    CLAIM_TYPE_INVALID = "claim_type_invalid"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_NOT_YET_VALID = "token_not_yet_valid"
    TOKEN_ISSUED_IN_FUTURE = "token_issued_in_future"
    TOKEN_TOO_OLD = "token_too_old"
    ISSUER_MISMATCH = "issuer_mismatch"
    AUDIENCE_MISMATCH = "audience_mismatch"
    AUTHORIZED_PARTY_MISMATCH = "authorized_party_mismatch"
    NONCE_MISMATCH = "nonce_mismatch"
    IDENTITY_INVALID = "identity_invalid"
    ROLES_INVALID = "roles_invalid"


class TokenAuthenticationError(PermissionError):
    """Safe authentication failure that never embeds a token or raw claim."""

    def __init__(self, code: AuthenticationErrorCode) -> None:
        super().__init__("Le jeton d'authentification est invalide")
        self.code = code


@runtime_checkable
class SignatureVerifier(Protocol):
    """Provider-neutral signature verification boundary."""

    def verify(
        self,
        signing_input: bytes,
        signature: bytes,
        *,
        algorithm: str,
        key_id: str | None,
    ) -> bool:
        """Return True only when the cryptographic signature is valid."""


@dataclass(frozen=True, slots=True)
class HmacSha256Verifier:
    """Offline HS256 verifier intended for local or symmetric deployments."""

    keys: Mapping[str, bytes] = field(repr=False)
    default_key_id: str | None = None

    def __post_init__(self) -> None:
        copied = dict(self.keys)
        if not copied:
            raise ValueError("Au moins une clé HMAC est obligatoire")
        if any(not key_id.strip() for key_id in copied):
            raise ValueError("Les identifiants de clé ne peuvent pas être vides")
        if any(len(secret) < 32 for secret in copied.values()):
            raise ValueError("Chaque clé HMAC doit contenir au moins 32 octets")
        if self.default_key_id is not None and self.default_key_id not in copied:
            raise ValueError("default_key_id doit désigner une clé connue")
        object.__setattr__(self, "keys", copied)

    def __repr__(self) -> str:
        return (
            "HmacSha256Verifier("
            f"key_ids={tuple(sorted(self.keys))!r}, default_key_id={self.default_key_id!r}, "
            "keys=<redacted>)"
        )

    def verify(
        self,
        signing_input: bytes,
        signature: bytes,
        *,
        algorithm: str,
        key_id: str | None,
    ) -> bool:
        if algorithm != "HS256":
            return False
        selected = key_id if key_id is not None else self.default_key_id
        if selected is None:
            return False
        secret = self.keys.get(selected)
        if secret is None:
            return False
        expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
        return hmac.compare_digest(expected, signature)


@dataclass(frozen=True, slots=True)
class TokenValidationPolicy:
    """Fail-closed rules applied after signature verification."""

    issuer: str
    audiences: frozenset[str]
    algorithms: frozenset[str]
    authentication_method: AuthenticationMethod = AuthenticationMethod.OIDC
    tenant_claim: str = "tenant_id"
    roles_claim: str = "roles"
    required_roles: frozenset[str] = frozenset()
    required_nonce: str | None = None
    leeway_seconds: float = 30.0
    max_token_age_seconds: float | None = 3600.0
    require_expiration: bool = True

    def __post_init__(self) -> None:
        if not self.issuer.strip():
            raise ValueError("issuer est obligatoire")
        if not self.audiences or any(not item.strip() for item in self.audiences):
            raise ValueError("Au moins une audience non vide est obligatoire")
        if (
            not self.algorithms
            or "none" in {item.casefold() for item in self.algorithms}
            or any(not item.strip() for item in self.algorithms)
        ):
            raise ValueError("Les algorithmes autorisés doivent être explicites")
        if self.authentication_method not in {
            AuthenticationMethod.JWT,
            AuthenticationMethod.OIDC,
        }:
            raise ValueError("La méthode doit être JWT ou OIDC")
        if not self.tenant_claim.strip() or not self.roles_claim.strip():
            raise ValueError("Les noms de claims ne peuvent pas être vides")
        if not 0.0 <= self.leeway_seconds <= 300.0:
            raise ValueError("leeway_seconds doit être compris entre 0 et 300")
        if (
            self.max_token_age_seconds is not None
            and not 0.0 < self.max_token_age_seconds <= 86_400.0
        ):
            raise ValueError(
                "max_token_age_seconds doit être compris entre 0 et 86 400"
            )


@dataclass(slots=True)
class JwtOidcAuthenticator:
    """Validate a compact JWT entirely offline and create an execution principal."""

    policy: TokenValidationPolicy
    verifier: SignatureVerifier
    clock: Callable[[], float] = time.time

    def authenticate(self, token: str) -> ExecutionPrincipal:
        encoded_header, encoded_claims, encoded_signature = self._segments(token)
        header = self._json_object(encoded_header, header=True)
        claims = self._json_object(encoded_claims, header=False)
        algorithm, key_id = self._validate_header(header)
        signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
        signature = self._decode_segment(encoded_signature)
        try:
            verified = self.verifier.verify(
                signing_input,
                signature,
                algorithm=algorithm,
                key_id=key_id,
            )
        except Exception as exc:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.SIGNATURE_INVALID
            ) from exc
        if not verified:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.SIGNATURE_INVALID
            )

        now = float(self.clock())
        if not math.isfinite(now):
            raise ValueError("L'horloge doit renvoyer une valeur finie")
        self._validate_time_claims(claims, now)
        self._validate_issuer(claims)
        self._validate_audience(claims)
        self._validate_nonce(claims)
        return self._principal(claims)

    @staticmethod
    def _segments(token: str) -> tuple[str, str, str]:
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.MALFORMED_TOKEN
            )
        parts = token.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.MALFORMED_TOKEN
            )
        if any(
            len(part) > _MAX_SEGMENT_BYTES or _BASE64URL.fullmatch(part) is None
            for part in parts
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.MALFORMED_TOKEN
            )
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _decode_segment(segment: str) -> bytes:
        padding = "=" * (-len(segment) % 4)
        try:
            return base64.b64decode(
                segment + padding,
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.MALFORMED_TOKEN
            ) from exc

    @classmethod
    def _json_object(cls, segment: str, *, header: bool) -> dict[str, object]:
        code = (
            AuthenticationErrorCode.INVALID_HEADER
            if header
            else AuthenticationErrorCode.MALFORMED_TOKEN
        )

        def reject_duplicates(
            pairs: Sequence[tuple[str, object]],
        ) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON member")
                result[key] = value
            return result

        try:
            decoded = json.loads(
                cls._decode_segment(segment).decode("utf-8"),
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise TokenAuthenticationError(code) from exc
        if not isinstance(decoded, dict):
            raise TokenAuthenticationError(code)
        return decoded

    def _validate_header(
        self, header: Mapping[str, object]
    ) -> tuple[str, str | None]:
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or not algorithm:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.INVALID_HEADER
            )
        if (
            algorithm.casefold() == "none"
            or algorithm not in self.policy.algorithms
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.UNSUPPORTED_ALGORITHM
            )
        if header.get("b64") is False or header.get("crit") not in (None, []):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.INVALID_HEADER
            )
        token_type = header.get("typ")
        if token_type is not None and (
            not isinstance(token_type, str)
            or token_type.casefold() not in {"jwt", "at+jwt"}
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.INVALID_HEADER
            )
        key_id = header.get("kid")
        if key_id is not None and (
            not isinstance(key_id, str)
            or not key_id.strip()
            or len(key_id) > 256
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.INVALID_HEADER
            )
        return algorithm, key_id

    def _validate_time_claims(
        self, claims: Mapping[str, object], now: float
    ) -> None:
        leeway = self.policy.leeway_seconds
        expiration = self._numeric_claim(
            claims, "exp", required=self.policy.require_expiration
        )
        if expiration is not None and now - leeway >= expiration:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.TOKEN_EXPIRED
            )
        not_before = self._numeric_claim(claims, "nbf", required=False)
        if not_before is not None and now + leeway < not_before:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.TOKEN_NOT_YET_VALID
            )
        issued_at = self._numeric_claim(
            claims,
            "iat",
            required=self.policy.max_token_age_seconds is not None,
        )
        if issued_at is not None and issued_at > now + leeway:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.TOKEN_ISSUED_IN_FUTURE
            )
        if (
            issued_at is not None
            and self.policy.max_token_age_seconds is not None
            and now - issued_at
            > self.policy.max_token_age_seconds + self.policy.leeway_seconds
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.TOKEN_TOO_OLD
            )

    @staticmethod
    def _numeric_claim(
        claims: Mapping[str, object], name: str, *, required: bool
    ) -> float | None:
        value = claims.get(name)
        if value is None:
            if required:
                raise TokenAuthenticationError(
                    AuthenticationErrorCode.REQUIRED_CLAIM_MISSING
                )
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.CLAIM_TYPE_INVALID
            )
        numeric = float(value)
        if not math.isfinite(numeric):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.CLAIM_TYPE_INVALID
            )
        return numeric

    def _validate_issuer(self, claims: Mapping[str, object]) -> None:
        issuer = claims.get("iss")
        if not isinstance(issuer, str):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.REQUIRED_CLAIM_MISSING
            )
        if issuer != self.policy.issuer:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.ISSUER_MISMATCH
            )

    def _validate_audience(self, claims: Mapping[str, object]) -> None:
        raw = claims.get("aud")
        if isinstance(raw, str):
            audiences = (raw,)
        elif (
            isinstance(raw, list)
            and raw
            and all(isinstance(item, str) and item for item in raw)
        ):
            audiences = tuple(raw)
        elif raw is None:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.REQUIRED_CLAIM_MISSING
            )
        else:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.CLAIM_TYPE_INVALID
            )
        matches = self.policy.audiences.intersection(audiences)
        if not matches:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.AUDIENCE_MISMATCH
            )
        if self.policy.authentication_method is AuthenticationMethod.OIDC:
            authorized_party = claims.get("azp")
            if len(audiences) > 1:
                if not isinstance(authorized_party, str):
                    raise TokenAuthenticationError(
                        AuthenticationErrorCode.REQUIRED_CLAIM_MISSING
                    )
                if authorized_party not in self.policy.audiences:
                    raise TokenAuthenticationError(
                        AuthenticationErrorCode.AUTHORIZED_PARTY_MISMATCH
                    )
            elif authorized_party is not None and (
                not isinstance(authorized_party, str)
                or authorized_party not in self.policy.audiences
            ):
                raise TokenAuthenticationError(
                    AuthenticationErrorCode.AUTHORIZED_PARTY_MISMATCH
                )

    def _validate_nonce(self, claims: Mapping[str, object]) -> None:
        if self.policy.required_nonce is None:
            return
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(
            nonce, self.policy.required_nonce
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.NONCE_MISMATCH
            )

    def _principal(self, claims: Mapping[str, object]) -> ExecutionPrincipal:
        subject = self._identity_claim(claims, "sub")
        tenant = self._identity_claim(claims, self.policy.tenant_claim)
        roles = self._roles(claims.get(self.policy.roles_claim))
        if not self.policy.required_roles.issubset(roles):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.ROLES_INVALID
            )
        try:
            return ExecutionPrincipal(
                subject_id=subject,
                tenant_id=tenant,
                authentication_method=self.policy.authentication_method,
                roles=roles,
            )
        except ValueError as exc:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.IDENTITY_INVALID
            ) from exc

    @staticmethod
    def _identity_claim(claims: Mapping[str, object], name: str) -> str:
        value = claims.get(name)
        if value is None:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.REQUIRED_CLAIM_MISSING
            )
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or any(ord(character) < 32 for character in value)
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.IDENTITY_INVALID
            )
        return value

    @staticmethod
    def _roles(value: object) -> frozenset[str]:
        if isinstance(value, str):
            candidates = value.split()
        elif isinstance(value, list):
            candidates = value
        else:
            raise TokenAuthenticationError(
                AuthenticationErrorCode.ROLES_INVALID
            )
        if (
            not candidates
            or any(
                not isinstance(role, str)
                or not role.strip()
                or len(role) > 128
                or any(ord(character) < 32 for character in role)
                for role in candidates
            )
        ):
            raise TokenAuthenticationError(
                AuthenticationErrorCode.ROLES_INVALID
            )
        return frozenset(candidates)
