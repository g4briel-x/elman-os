# Exécution IA résiliente et bornée

Le module `elman_os.execution` place une frontière de contrôle entre
l'orchestrateur ELMAN-OS et tout adaptateur `AIProvider`. Il n'effectue aucun
appel réseau lui-même et ne dépend d'aucun SDK fournisseur.

## Garanties

- chaque requête possède un délai global réel ;
- seules les erreurs `ProviderError` marquées `retryable=True` sont relancées ;
- le nombre de tentatives et chaque attente sont bornés ;
- `retry_after_seconds` est respecté sans dépasser le délai maximal configuré ;
- les annulations `asyncio.CancelledError` sont propagées immédiatement ;
- les identités de requête, fournisseur et modèle sont vérifiées au retour ;
- une réponse dépassant `max_output_tokens` est rejetée ;
- un même exécuteur partage un budget d'appels, de tokens et de durée ;
- un budget insuffisant bloque l'appel avant le fournisseur.

## Erreurs

Les adaptateurs continuent d'émettre `ProviderError` avec un
`ProviderErrorCode` portable. L'exécuteur ajoute quatre erreurs de Kernel :

| Code | Signification |
|---|---|
| `budget_exceeded` | un plafond d'appels, de tokens ou de durée est atteint |
| `deadline_exceeded` | le temps restant ne permet plus de poursuivre |
| `retry_exhausted` | toutes les tentatives autorisées ont échoué |
| `provider_contract` | la réponse ne respecte pas le contrat portable |

Une erreur non temporaire, par exemple `authentication` ou `invalid_request`,
est renvoyée immédiatement sans retry. Une erreur de timeout interne est
normalisée en `ProviderErrorCode.TIMEOUT`, puis suit la politique de retry.

## Politique de retry

`RetryPolicy` définit :

- `max_attempts` : de 1 à 10 ;
- `initial_delay_seconds` : première attente exponentielle ;
- `max_delay_seconds` : plafond de chaque attente ;
- `backoff_multiplier` : facteur compris entre 1 et 10.

Il n'y a pas de jitter aléatoire dans cette alpha afin que les tests et les
diagnostics restent déterministes. Un adaptateur peut fournir
`retry_after_seconds` ; la valeur est plafonnée par `max_delay_seconds`.

## Budget

`UsageBudget` impose :

- `max_provider_calls` ;
- `max_total_tokens` ;
- `max_elapsed_seconds`.

Avant un appel, le Kernel vérifie que le budget de tokens restant peut couvrir
les tokens d'entrée estimés et `max_output_tokens`. Après une réponse, la
consommation normalisée du fournisseur est enregistrée dans `BudgetLedger`.

Cette alpha ne calcule pas un montant monétaire : un coût fiable exige un
catalogue de prix versionné propre à chaque fournisseur et modèle. Les plafonds
d'appels, de tokens et de temps empêchent néanmoins une consommation non
bornée.

## Exemple

```python
from elman_os.execution import ResilientAIExecutor, RetryPolicy, UsageBudget
from elman_os.provider import DeterministicModelProvider

provider = DeterministicModelProvider()
executor = ResilientAIExecutor(
    provider,
    retry_policy=RetryPolicy(max_attempts=3),
    budget=UsageBudget(
        max_provider_calls=10,
        max_total_tokens=100_000,
        max_elapsed_seconds=300,
    ),
)

result = await executor.generate(request)
print(result.attempts)
print(result.usage.total_tokens)
```

Le `BudgetLedger` appartient à l'exécuteur. Créer un nouvel exécuteur crée un
nouveau périmètre budgétaire ; le service applicatif devra donc conserver et
partager l'instance correspondant au workflow à contrôler.
