# Adaptateur OpenAI et OpenAI-compatible

Le module `elman_os.openai_compatible` traduit le contrat stable ELMAN-OS vers
une API `chat/completions`. Il utilise uniquement la bibliothèque standard et
ne contacte aucun service pendant son import, son enregistrement ou sa
construction.

## Profils

| Fournisseur | URL |
|---|---|
| `openai` | `https://api.openai.com/v1` par défaut, surcharge HTTPS possible |
| `openai-compatible` | `ELMAN_AI_BASE_URL` explicite et obligatoire |

Les modèles ne sont pas figés dans le bundle : `ELMAN_AI_MODEL` fournit
l'identifiant transmis à l'endpoint. Cette flexibilité ne garantit pas qu'un
modèle existe réellement chez un fournisseur.

## Transport injectable

`AsyncHTTPTransport` expose deux opérations :

- `send(HTTPRequest) -> HTTPResponse` ;
- `close()`.

`UrllibAsyncTransport` est l'implémentation standard. Les tests injectent
`FakeTransport`, qui conserve les requêtes en mémoire et retourne des réponses
préparées. Ainsi, la traduction complète et le pipeline résilient sont testés
sans DNS, socket, clé réelle ou coût.

## Traduction

Une `ModelRequest` devient une requête `POST` vers
`/chat/completions` contenant :

- `model` ;
- `messages` avec `role`, `content` et `name` facultatif ;
- `max_completion_tokens` pour `openai`, ou `max_tokens` pour le profil
  compatible ;
- `temperature`.

La réponse doit contenir un premier choix textuel. L'identifiant fournisseur,
le modèle, la raison de terminaison et les tokens d'entrée/sortie sont convertis
vers `ModelResponse`.

## Classification des erreurs

| Origine | Code portable | Retry |
|---|---|---|
| 400, 409, 422 | `invalid_request` | non |
| 401 | `authentication` | non |
| 403 | `authorization` | non |
| 404 | `model_not_found` | non |
| 408 | `timeout` | oui |
| 429 | `rate_limited` | oui |
| 5xx | `service_unavailable` | oui |
| transport | `network` | oui |
| JSON invalide | `unknown` | non |

Le corps d'une erreur distante n'est jamais recopié dans le message portable.
Un `Retry-After` numérique est conservé seulement s'il est compris entre 0 et
300 secondes.

## Configuration PowerShell

Ne pas conserver la clé dans un fichier suivi par Git :

```powershell
$env:ELMAN_AI_PROVIDER = "openai-compatible"
$env:ELMAN_AI_MODEL = "compatible-model-id"
$env:ELMAN_AI_AUTH_MODE = "api_key"
$env:ELMAN_AI_API_KEY = "<secret-local>"
$env:ELMAN_AI_BASE_URL = "https://api.provider.example/v1"

.\.venv\Scripts\python.exe -m elman_os ai-config
.\.venv\Scripts\python.exe -m elman_os ai-providers
```

Ces commandes valident et affichent la configuration sans effectuer
d'inférence. `credential_configured` indique uniquement si une clé existe.

## Limites

- seul le texte non-streamé est pris en charge ;
- les tool calls, images et réponses structurées ne sont pas encore traduits ;
- la compatibilité publiée est contractuelle et hors réseau ;
- aucun endpoint ou modèle distant n'est certifié par cette alpha.
