# Authentification JWT/OIDC hors réseau

## Objectif

Ce lot ajoute une frontière d’authentification cryptographique avant
`ExecutionPrincipal`. Il ne remplace ni l’autorisation par rôles, ni les quotas,
ni l’audit HMAC existants.

## Garanties

- aucune découverte OIDC et aucun appel réseau ;
- refus explicite de `alg=none` et des algorithmes non autorisés ;
- validation de la signature avant toute utilisation des claims ;
- validation de `iss`, `aud`, `exp`, `nbf`, `iat`, `azp` et du `nonce` attendu ;
- durée maximale du jeton et tolérance d’horloge bornées ;
- rejet des membres JSON dupliqués, valeurs non finies et segments malformés ;
- conversion vers `ExecutionPrincipal` avec tenant et rôles vérifiés ;
- erreurs normalisées qui ne reproduisent jamais le jeton ni les claims.

## Exemple local HS256

```python
from elman_os.authentication import (
    HmacSha256Verifier,
    JwtOidcAuthenticator,
    TokenValidationPolicy,
)

policy = TokenValidationPolicy(
    issuer="https://identity.example",
    audiences=frozenset({"elman-api"}),
    algorithms=frozenset({"HS256"}),
    required_roles=frozenset({"ai.execute"}),
)
verifier = HmacSha256Verifier(
    {"active": secret_bytes},
    default_key_id="active",
)
principal = JwtOidcAuthenticator(policy, verifier).authenticate(compact_token)
```

La clé doit provenir d’un gestionnaire de secrets. Elle ne doit jamais être
commise dans le dépôt.

## Fournisseur OIDC avec clés asymétriques

La récupération et la rotation du JWKS appartiennent à la couche
d’infrastructure. La couche cœur reçoit un objet qui implémente :

```python
class SignatureVerifier:
    def verify(
        self,
        signing_input: bytes,
        signature: bytes,
        *,
        algorithm: str,
        key_id: str | None,
    ) -> bool: ...
```

Un adaptateur RS256/ES256 peut ainsi utiliser une bibliothèque cryptographique
optionnelle et un cache JWKS alimenté en dehors de la validation. Le validateur
reste déterministe et testable hors réseau.

## Claims attendus

| Claim | Règle |
| --- | --- |
| `iss` | correspondance exacte avec l’issuer configuré |
| `aud` | chaîne ou tableau contenant une audience autorisée |
| `sub` | identité non vide, bornée et sans caractère de contrôle |
| `tenant_id` | tenant non vide ; nom configurable |
| `roles` | tableau de chaînes ou chaîne séparée par espaces |
| `exp` | obligatoire par défaut |
| `iat` | obligatoire lorsque la durée maximale est activée |
| `nbf` | facultatif, mais validé lorsqu’il est présent |
| `azp` | obligatoire en OIDC lorsque plusieurs audiences sont présentes |
| `nonce` | validé en temps constant lorsqu’une valeur est exigée |

## Limites du lot

Ce composant ne télécharge pas un document de découverte OIDC ou un JWKS. Il ne
gère pas non plus l’émission, le rafraîchissement ou la révocation des jetons.
Ces fonctions seront branchées ultérieurement autour de l’interface
`SignatureVerifier`.
