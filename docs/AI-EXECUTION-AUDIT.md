# Authentification et audit des exécutions IA

## Objectif

`audit.py` place une enveloppe de sécurité autour de
`ResilientAIExecutor`. Elle vérifie l'identité et l'autorisation avant tout
appel fournisseur, puis produit une trace minimale dont l'intégrité peut être
contrôlée.

Ce module ne remplace pas un serveur OIDC, JWT ou API key. La frontière
d'authentification de l'application vérifie le credential, puis construit un
`ExecutionPrincipal`. Le Kernel refuse les méthodes anonymes et exige le rôle
`ai.execute`.

## Événements

Chaque exécution autorisée produit :

1. `started` ;
2. `succeeded`, `failed` ou `cancelled`.

Une demande refusée produit seulement `denied`, avant tout appel fournisseur.
Si la première écriture d'audit échoue, l'exécution échoue elle aussi.

## Données conservées

- empreintes HMAC du principal, du tenant et de la requête ;
- identifiant de corrélation validé ;
- méthode d'authentification et motif contrôlé ;
- fournisseur, modèle et résultat ;
- tentatives, tokens et durée ;
- code d'erreur portable, le cas échéant.

Le schéma ne possède aucun champ pour les prompts, réponses, secrets,
métadonnées libres ou identifiants de requête fournisseur.

## Intégrité

`AuditSigner` utilise HMAC-SHA-256 avec une clé d'au moins 32 octets. Chaque
signature couvre l'événement canonique et la signature précédente. Une
modification, une réorganisation ou une suppression au milieu de la chaîne est
donc détectable par `AuditTrail.verify_chain()`.

`FileAuditSink` fournit depuis l’alpha.7 une persistance JSONL append-only avec
synchronisation durable. `AuditTrail.resume()` recharge le fichier, vérifie la
chaîne entière et reprend depuis la dernière signature valide. Une ligne
illisible ou altérée provoque un échec fermé.

La clé doit provenir d'un gestionnaire de secrets en production. Elle ne doit
jamais être stockée dans Git, dans un fichier d'exemple ou dans la trace.

## Limites de l'alpha

- `InMemoryAuditSink` et `FileAuditSink` sont fournis ; le second reste local
  et mono-machine ;
- la validation cryptographique d'un JWT/OIDC reste à la frontière applicative ;
- la rotation des clés et l'ancrage externe des signatures ne sont pas livrés ;
- aucun journal distant ni SIEM n'est contacté ;
- la rotation du fichier n'est pas livrée.

Commande de diagnostic, sans clé et sans appel réseau :

```powershell
.\.venv\Scripts\python.exe -m elman_os ai-audit
```
