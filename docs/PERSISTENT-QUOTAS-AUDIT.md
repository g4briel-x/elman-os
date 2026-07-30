# Quotas et audit persistants multi-instance

## Objectif

Ce lot remplace les états de gouvernance limités à un seul processus par deux
composants adossés au contrat `PersistenceBackend` :

- `PersistentIdentityQuotaManager` partage les plafonds de requêtes, de tokens
  et de concurrence entre plusieurs instances ;
- `PersistentAuditTrail` construit une chaîne HMAC atomique et vérifiable par
  tenant, même lorsque plusieurs processus écrivent dans la même base.

SQLite reste utilisable localement. Un futur adaptateur PostgreSQL pourra
implémenter le même contrat sans modifier ces composants.

## Quotas

Le gestionnaire persistant conserve l'interface asynchrone `reserve`, `settle`
et `snapshot` déjà consommée par `StabilizedAIExecutor`.

```python
quotas = PersistentIdentityQuotaManager(
    tenant_id="tenant-a",
    backend=backend,
    quota=IdentityQuota(
        max_requests=100,
        max_tokens=1_000_000,
        max_concurrent=4,
    ),
)
```

Chaque réservation est atomique. Une durée de vie bornée permet de récupérer
la capacité réservée par un processus interrompu avant `settle`.

## Audit

`PersistentAuditTrail` est compatible avec `AuditedAIExecutor` :

```python
trail = PersistentAuditTrail(
    signer=AuditSigner(signing_key),
    backend=backend,
    tenant_id="tenant-a",
)
audited = AuditedAIExecutor(executor, trail)
```

L'événement et l'état de chaîne sont écrits dans une même transaction. Le
composant refuse :

- un événement appartenant à un autre tenant ;
- une clé HMAC différente de celle qui a initialisé la chaîne ;
- un état ou un événement persistant mal formé.

La méthode `verify_persisted()` contrôle la continuité, chaque signature et la
cohérence entre le dernier événement et l'état de chaîne.

## Sécurité et exploitation

- Aucun identifiant brut n'est ajouté aux événements d'audit.
- Les quotas restent indexés par empreinte HMAC d'identité.
- Les namespaces et tenants sont isolés par la couche transactionnelle.
- Les écritures échouent de façon fermée si la persistance est indisponible.
- La même clé d'audit doit être fournie à toutes les instances d'un tenant.
