# Configuration sécurisée du fournisseur IA

ELMAN-OS charge la configuration du runtime IA depuis l'environnement du
processus. Le Kernel ne lit pas automatiquement de fichier `.env`, n'écrit
aucune clé sur disque et n'effectue aucun appel réseau pendant le chargement.

## Variables reconnues

| Variable | Valeur par défaut | Rôle |
|---|---|---|
| `ELMAN_AI_PROVIDER` | `deterministic-model` | Adaptateur à sélectionner |
| `ELMAN_AI_MODEL` | `deterministic-v1` | Modèle demandé |
| `ELMAN_AI_AUTH_MODE` | `none` en mode déterministe, sinon `api_key` | Mode d'authentification |
| `ELMAN_AI_API_KEY` | aucune | Secret requis en mode `api_key` |
| `ELMAN_AI_BASE_URL` | aucune | URL HTTPS optionnelle de l'API |
| `ELMAN_AI_TIMEOUT_SECONDS` | `60` | Délai borné, supérieur à 0 et inférieur ou égal à 600 |
| `ELMAN_AI_MAX_OUTPUT_TOKENS` | `2048` | Limite comprise entre 1 et 1 000 000 |

Une URL HTTP est acceptée uniquement pour `localhost`, `127.0.0.1` ou `::1`,
afin de permettre un modèle local. Une URL ne peut pas embarquer
d'identifiant, de mot de passe, de paramètres ou de fragment.

## Définition temporaire sous PowerShell

Le mode déterministe ne nécessite aucune clé :

```powershell
$env:ELMAN_AI_PROVIDER = "deterministic-model"
$env:ELMAN_AI_MODEL = "deterministic-v1"
$env:ELMAN_AI_AUTH_MODE = "none"

.\.venv\Scripts\python.exe -m elman_os ai-config
```

Pour un futur adaptateur distant :

```powershell
$env:ELMAN_AI_PROVIDER = "vendor"
$env:ELMAN_AI_MODEL = "vendor-model"
$env:ELMAN_AI_AUTH_MODE = "api_key"
$env:ELMAN_AI_API_KEY = Read-Host "Clé API"

.\.venv\Scripts\python.exe -m elman_os ai-config
```

La saisie ci-dessus reste limitée au processus PowerShell courant. La commande
de diagnostic affiche seulement `credential_configured: true`, jamais la clé.

Pour retirer la clé de la session :

```powershell
Remove-Item Env:ELMAN_AI_API_KEY -ErrorAction SilentlyContinue
```

## Frontière de sécurité

`SecretValue` masque le secret dans `str`, `repr`, les dataclasses et le résumé
de diagnostic. Seul un adaptateur fournisseur, au moment de construire
l'en-tête d'authentification, devra appeler explicitement `reveal()`.

Cette protection réduit les fuites accidentelles ; elle ne remplace pas un
gestionnaire de secrets du système d'exploitation ou de la plateforme de
déploiement.
