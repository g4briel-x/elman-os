# Contrat générique de fournisseur IA — ELMAN-OS v0.4 alpha 1

## Objectif

`src/elman_os/provider.py` définit une frontière stable entre le Kernel
ELMAN-OS et les services de modèles IA. Le Kernel dépend de ce contrat, jamais
du SDK d'un fournisseur particulier.

Ce premier lot couvre uniquement l'inférence texte asynchrone. Il ne réalise
aucun appel réseau et ne lit aucune clé API.

## Contrats

| Type | Rôle |
|---|---|
| `AIProvider` | protocole minimal de tout adaptateur IA |
| `ProviderDescriptor` | identité, modèles et capacités déclarées |
| `ModelMessage` | message typé `system`, `user`, `assistant` ou `tool` |
| `ModelRequest` | requête bornée en tokens, température et délai |
| `ModelResponse` | texte, raison d'arrêt, usage et identifiants de traçabilité |
| `TokenUsage` | consommation d'entrée, de sortie et total calculé |
| `ProviderError` | erreur normalisée et décision de retry explicite |
| `DeterministicModelProvider` | double de test local sans réseau ni coût |

## Invariants

1. Toute génération est asynchrone.
2. Toute requête possède un identifiant ELMAN indépendant de l'identifiant du
   fournisseur.
3. Le modèle, les messages, le plafond de sortie, la température et le délai
   sont explicites.
4. Une capacité non déclarée est refusée avant toute génération.
5. Les erreurs fournisseur sont converties en codes portables.
6. Le caractère retentable d'une erreur est explicite ; le Kernel ne le devine
   pas à partir d'un texte.
7. `close()` libère les transports détenus par l'adaptateur.
8. Aucun secret brut ne doit être placé dans `metadata`, les messages,
   les réponses ou les exceptions.

## Exemple sans appel API

```python
import asyncio

from elman_os.provider import (
    DeterministicModelProvider,
    MessageRole,
    ModelMessage,
    ModelRequest,
)


async def main() -> None:
    provider = DeterministicModelProvider(("Architecture proposée.",))
    response = await provider.generate(
        ModelRequest(
            request_id="demo-001",
            model="deterministic-v1",
            messages=(
                ModelMessage(MessageRole.USER, "Proposer une architecture."),
            ),
        )
    )
    print(response.content)
    await provider.close()


asyncio.run(main())
```

## Responsabilité des futurs adaptateurs

Un adaptateur OpenAI, Anthropic, Google ou local devra :

- traduire `ModelRequest` vers son SDK ;
- appliquer `timeout_seconds` ;
- convertir la réponse en `ModelResponse` ;
- convertir toutes les erreurs en `ProviderError` sans exposer de secret ;
- déclarer honnêtement ses capacités et modèles configurés ;
- fermer ses clients réseau dans `close()` ;
- ne jamais choisir seul un retry, un budget ou un modèle de remplacement.

Les retries bornés, les budgets, la configuration par environnement, la
sélection multi-fournisseurs et le routage agentique appartiennent aux lots
v0.4 suivants.
