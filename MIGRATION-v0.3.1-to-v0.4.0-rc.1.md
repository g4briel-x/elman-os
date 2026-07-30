# Migration v0.3.1 → v0.4.0-rc.1

## Portée

La branche v0.4 ajoute un Kernel IA optionnel. Les fonctions v0.3.1 de
planification, génération, workflow, plugins, approbations et persistance
SQLite restent compatibles.

## Procédure sûre sous Windows

Depuis une branche dédiée :

```powershell
git switch feature/v0.4-kernel-ai-runtime
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v
```

Résultat attendu :

```text
Ran 180 tests
OK
```

Puis :

```powershell
.\.venv\Scripts\python.exe -m elman_os ai-config
.\.venv\Scripts\python.exe -m elman_os ai-providers
.\.venv\Scripts\python.exe -m elman_os ai-audit
.\.venv\Scripts\python.exe -m elman_os ai-readiness
.\.venv\Scripts\python.exe -m elman_os release-check .
.\.venv\Scripts\python.exe -m elman_os doctor
```

## Configuration

Le mode par défaut reste :

```text
provider_id: deterministic-model
model: deterministic-v1
auth_mode: none
```

Il ne nécessite aucune clé et n’ouvre pas le réseau. Ne configurez un fournisseur
distant qu’après validation organisationnelle du stockage des secrets, des
quotas, des coûts et de la politique de données.

## Audit persistant

La persistance doit utiliser un emplacement privé et sauvegardé. La clé HMAC
doit provenir d’un gestionnaire de secrets et contenir au moins 32 octets.
Elle ne doit être ni committée, ni inscrite dans un fichier `.env` versionné.

## Compatibilité applicative

Les imports historiques restent disponibles. Les nouveaux points d’entrée sont :

- `StabilizedAIRuntime` ;
- `IdentityQuotaManager` avec réservations non rejouables et isolation par tenant ;
- `FileAuditSink` ;
- `check_configuration_compatibility`.

## Retour arrière

Le retour à v0.3.1 consiste à changer de branche ou de tag. Aucun schéma SQLite
v0.3.1 n’est modifié par ce lot. Conservez le journal d’audit v0.4 séparément :
sa suppression détruirait la continuité de preuve.
