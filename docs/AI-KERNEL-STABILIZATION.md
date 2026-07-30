# Stabilisation du Kernel IA — v0.4.0

## Objectif

Ce socle ferme le périmètre fonctionnel du Kernel IA livré dans v0.4.0.
Il compose les contrôles déjà livrés — configuration, registre, résilience,
adaptateurs et audit — avec trois garanties supplémentaires :

1. une prévalidation complète avant création du fournisseur ;
2. des quotas atomiques et indépendants par identité pseudonymisée ;
3. un journal d’audit persistant, durable et vérifiable après redémarrage.

## Prévalidation de configuration

`check_configuration_compatibility()` compare `ProviderSettings` aux descriptors
du registre sans créer d’adaptateur et sans ouvrir le réseau. Le contrôle couvre :

- présence du fournisseur ;
- déclaration du modèle et des capacités ;
- compatibilité du mode d’authentification ;
- absence de paramètres distants pour le fournisseur déterministe ;
- URL explicite pour un fournisseur `openai-compatible`.

Le rapport est sérialisable et ne contient jamais de clé.

## Quotas par identité

`IdentityQuotaManager` utilise exclusivement une empreinte HMAC isolée par
tenant et identité. Chaque réservation possède un identifiant unique ; un
règlement rejoué, forgé ou déjà consommé est refusé.
Les identifiants bruts ne sont jamais stockés dans les compteurs.

Les limites portent sur :

- le nombre total de requêtes ;
- les tokens consommés et réservés ;
- le nombre d’exécutions simultanées.

La réservation est atomique. Une annulation, un échec ou une réussite libère
toujours la place concurrente dans un bloc `finally`. Un dépassement est refusé
avant le fournisseur et produit un événement d’audit minimal `denied`.

## Audit persistant

`FileAuditSink` écrit un événement JSON canonique par ligne :

- ouverture append-only ;
- création en permissions restreintes lorsque la plateforme le permet ;
- synchronisation durable après chaque événement ;
- plafond de taille configurable ;
- refus des liens symboliques ;
- aucune donnée de prompt, réponse, secret ou metadata libre.

`AuditTrail.resume()` recharge le journal, vérifie l’intégralité de la chaîne
HMAC puis reprend à partir de la dernière signature. Toute ligne illisible,
altérée, supprimée ou réordonnée provoque un échec fermé.

## Composition

`StabilizedAIRuntime.from_settings()` assemble :

```text
configuration → compatibilité → registre → fournisseur → résilience
→ autorisation → quota identité → audit persistant → résultat
```

Le fournisseur déterministe permet de valider ce pipeline de bout en bout sans
clé, réseau ou coût.

## Diagnostic

```powershell
.\.venv\Scripts\python.exe -m elman_os ai-readiness
```

La sortie confirme la prévalidation, les quotas, la persistance d’audit et le
statut de la version stable. Elle n’effectue aucun appel IA.

## Limites

- les quotas sont process-local dans cette alpha ;
- le partage multi-instance nécessitera un backend transactionnel externe ;
- la rotation et l’archivage du journal seront traités après publication de la version stable ;
- les adaptateurs réels restent validés uniquement avec transports simulés.
