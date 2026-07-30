# Registre et sélection des fournisseurs IA

Le module `elman_os.registry` relie la configuration sécurisée, le contrat
`AIProvider` et l'exécuteur résilient. Il ne contient aucun SDK distant et
n'effectue aucun appel réseau.

## Responsabilités

| Type | Rôle |
|---|---|
| `ProviderRegistration` | associer un descriptor déclaré à une factory |
| `ProviderRegistry` | enregistrer et résoudre les fournisseurs disponibles |
| `ProviderSelection` | tracer le choix demandé, le choix réel et le fallback |
| `ConfiguredAIRuntime` | conserver le fournisseur choisi et son budget partagé |
| `ProviderRegistryError` | exposer une erreur de sélection portable |

Le registre intégré déclare `deterministic-model`, `openai` et
`openai-compatible`. La déclaration n'effectue aucun appel réseau. Le transport
réel ou simulé n'est utilisé qu'au moment de `generate()`.

## Règles de sélection

1. `ELMAN_AI_PROVIDER` et `ELMAN_AI_MODEL` désignent le fournisseur et le modèle
   demandés.
2. Le descriptor est contrôlé avant la création de l'adaptateur.
3. Le modèle et toutes les capacités requises doivent être déclarés.
4. La factory doit produire un objet conforme à `AIProvider`.
5. Le descriptor de l'instance doit être exactement celui enregistré.
6. Un fournisseur inconnu ou temporairement indisponible échoue par défaut.
7. Le fallback déterministe exige
   `allow_deterministic_fallback=True`.
8. Une incompatibilité de modèle ou de capacité n'est jamais masquée par un
   fallback.

## Exemple local

```python
from elman_os.configuration import load_provider_settings
from elman_os.registry import ConfiguredAIRuntime, built_in_provider_registry

settings = load_provider_settings({})
runtime = ConfiguredAIRuntime.from_settings(
    built_in_provider_registry(),
    settings,
)

result = await runtime.generate(request)
print(result.response.content)
await runtime.close()
```

## Fallback contrôlé

Le fallback est destiné au développement local, aux démonstrations et aux
tests. Il remplace à la fois le fournisseur et le modèle par
`deterministic-model` / `deterministic-v1`, puis trace :

- le fournisseur et le modèle demandés ;
- le fournisseur et le modèle sélectionnés ;
- la raison portable du fallback ;
- `used_fallback: true`.

Il ne réutilise ni la clé, ni l'URL, ni le mode d'authentification du fournisseur
distant. `safe_summary()` n'affiche aucun secret.

```python
runtime = ConfiguredAIRuntime.from_settings(
    built_in_provider_registry(),
    settings,
    allow_deterministic_fallback=True,
)
```

Une application de production doit laisser le fallback désactivé, sauf décision
explicite et observable de sa politique d'exécution.

## Adaptateurs intégrés

`openai` utilise `https://api.openai.com/v1` par défaut.
`openai-compatible` exige une valeur explicite et validée dans
`ELMAN_AI_BASE_URL`. Les deux utilisent le même contrat
`OpenAICompatibleProvider`, décrit dans `AI-OPENAI-COMPATIBLE.md`.

## Limite de cette alpha

Le registre et les adaptateurs sont exécutables, mais la validation publiée est
entièrement hors réseau. Elle ne certifie aucun endpoint, modèle, quota,
tarification ou comportement futur d'un service distant.
