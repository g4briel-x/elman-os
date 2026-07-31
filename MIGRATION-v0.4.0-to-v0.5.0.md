# Migration ELMAN-OS v0.4.0 → v0.5.0

## Portée

La version `v0.5.0` conserve les API publiques de `v0.4.0` et ajoute des
frontières de production optionnelles :

- validation JWT/OIDC hors réseau ;
- persistance transactionnelle isolée par tenant ;
- quotas et audit persistants partagés entre instances ;
- runtime d’exécution authentifié ;
- route FastAPI `POST /v1/ai/generate`.

`SQLiteKernelStore`, les workflows, les approbations et le runtime déterministe
historique restent disponibles.

## Validation avant migration

```powershell
git status -sb
git branch --show-current
.\.venv\Scripts\python.exe -m elman_os release-check .
```

Le dépôt doit être propre et la version source doit être `0.4.0`.

## Données

La nouvelle persistance utilise ses propres tables et ne modifie pas les tables
historiques de `SQLiteKernelStore`. Une sauvegarde du fichier SQLite reste
obligatoire avant toute activation sur des données réelles.

Chaque opération du nouveau backend exige un `tenant_id`. Une valeur de tenant
ne doit jamais provenir directement du corps HTTP : le runtime la prend dans le
principal JWT/OIDC authentifié.

## Authentification

La politique doit définir explicitement l’émetteur, l’audience, les algorithmes,
les tolérances temporelles et le rôle `ai.execute`. Les clés de signature et la
clé HMAC d’audit doivent provenir d’un gestionnaire de secrets ; elles ne sont
jamais committées.

## Compatibilité

Les imports `v0.4.0` restent valides. Les nouveaux points d’entrée principaux
sont :

- `JwtOidcAuthenticator` ;
- `SQLitePersistence` ;
- `PersistentIdentityQuotaManager` ;
- `PersistentAuditTrail` ;
- `ProductionAIRuntime` ;
- `AuthenticatedExecutionService`.

## Retour arrière

1. arrêter les instances utilisant le runtime `v0.5.0` ;
2. conserver une copie de la base et du journal d’audit ;
3. revenir au tag `v0.4.0` ;
4. réinstaller le paquet dans un environnement neuf.

Le code `v0.4.0` ignore les nouvelles tables. Ne les supprimez pas : leur
suppression détruirait l’historique de quotas et la continuité de preuve.

## Gates maintenues

La migration ne constitue pas une autorisation de déploiement. La connectivité
réelle des fournisseurs, PostgreSQL, la rotation opérationnelle des clés,
l’isolation de processus et les procédures d’incident doivent encore être
validées dans l’environnement cible.
