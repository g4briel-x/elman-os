# Runtime d’exécution de production

## Objectif

Le runtime compose systématiquement les frontières déjà validées :

1. authentification JWT/OIDC hors réseau ;
2. autorisation par rôle et motif ;
3. quota atomique persistant, isolé par tenant ;
4. exécution résiliente du fournisseur ;
5. audit HMAC persistant, chaîné et isolé par tenant.

Une requête ne peut plus contourner les quotas ou l’audit en utilisant
directement le runtime de production.

## Composants

- `PersistentGovernedAIExecutor` crée les vues de quota et d’audit propres au
  tenant du principal authentifié.
- `AuthenticatedExecutionService` transforme un jeton validé en contexte
  d’exécution.
- `ProductionAIRuntime` possède le fournisseur et, facultativement, le backend.
- `attach_execution_routes` ajoute `POST /v1/ai/generate` à l’API FastAPI.

## Propriétés de sécurité

- Le tenant provient uniquement du principal authentifié.
- Le modèle configuré remplace tout modèle fourni par le client.
- Un échec d’écriture d’audit bloque l’appel au fournisseur.
- Les quotas sont partagés entre les processus utilisant le même backend.
- Aucun prompt, contenu de réponse, jeton ou identifiant brut n’est journalisé.
- Les erreurs HTTP sont normalisées et ne contiennent pas de secret.

## Cycle de vie

Utiliser `async with ProductionAIRuntime...` ou appeler `await runtime.close()`.
La fermeture est idempotente. Le fournisseur est toujours fermé ; le backend
est fermé lorsque le runtime en est propriétaire.

## API

Lorsque `create_app(..., execution_service=service)` reçoit le service
d’exécution, la route suivante est attachée :

```text
POST /v1/ai/generate
Authorization: Bearer <jeton>
```

Le corps accepte `messages`, `request_id`, `max_output_tokens`,
`temperature`, `timeout_seconds`, `purpose` et `correlation_id`.
