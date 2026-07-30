# Persistance transactionnelle ELMAN-OS

## Compatibilité avec le stockage historique

`src/elman_os/persistence.py` et sa classe `SQLiteKernelStore` restent
inchangés. La nouvelle frontière vit dans
`src/elman_os/transactional_persistence.py`. Les deux mécanismes peuvent
coexister pendant la migration progressive des composants ELMAN-OS.

## Objectif

Ce lot fournit une frontière de persistance indépendante du moteur de base de
données. Il conserve SQLite pour le développement local et prépare
l'introduction ultérieure d'un adaptateur PostgreSQL multi-instance.

## Garanties

- isolation par `tenant_id` et `namespace` dans chaque requête ;
- transaction atomique avec commit ou rollback ;
- verrouillage optimiste au moyen de `expected_version` ;
- valeurs JSON strictes, bornées à 1 Mio ;
- identifiants validés avant toute requête SQL ;
- requêtes paramétrées ;
- aucune dépendance Python supplémentaire ;
- aucune connexion réseau.

## Sémantique de version

| `expected_version` | Effet |
| --- | --- |
| `0` | création uniquement ; conflit si la clé existe |
| entier positif | mise à jour uniquement si la version correspond |
| `None` | création ou mise à jour inconditionnelle |

## Exemple

```python
from elman_os.transactional_persistence import SQLitePersistence

store = SQLitePersistence("var/elman-os.sqlite3")

async with store.transaction("tenant-001", "agents") as tx:
    agent = await tx.put(
        "agent-001",
        {"status": "ready"},
        expected_version=0,
    )

async with store.transaction("tenant-001", "agents") as tx:
    current = await tx.get("agent-001")
    updated = await tx.put(
        "agent-001",
        {"status": "running"},
        expected_version=current.version,
    )
```

## Extension PostgreSQL

Un adaptateur PostgreSQL devra implémenter `PersistenceBackend` et
`PersistenceTransaction`, utiliser une transaction de base réelle, conserver la
clé composite `(tenant_id, namespace, record_key)` et respecter exactement la
sémantique de version décrite ci-dessus. Le noyau métier ne devra pas importer
de pilote PostgreSQL.
