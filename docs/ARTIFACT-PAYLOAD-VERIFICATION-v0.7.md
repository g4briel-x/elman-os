# Artifact Payload Verification ELMAN-OS v0.7

## Statut

Dixième incrément du Jalon 2 — Planification et orchestration.

Ce module vérifie les octets réels reçus en mémoire avant toute interaction
avec le workspace. Il lie chaque payload à une opération du
`ArtifactApplicationPlan`, recalcule la taille et le SHA-256, valide le chemin
et le type de média, puis produit un résultat déterministe.

Aucun fichier n’est lu, créé, modifié, supprimé ou exécuté.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_payload_verification.py` | Définit les payloads, la politique, la requête, les enregistrements et le résultat de vérification. |
| `tests/test_artifact_payload_verification.py` | Vérifie les octets, hashes, tailles, médias, doublons, payloads manquants, limites et sérialisations. |
| `docs/ARTIFACT-PAYLOAD-VERIFICATION-v0.7.md` | Documente les contrats, décisions, contrôles et limites. |

## Contrats

### `ArtifactPayload`

Un payload contient :

- l’identifiant exact de l’opération ;
- le chemin de destination ;
- le type de média ;
- les octets réels ;
- un hash SHA-256 du contrat complet.

Les propriétés calculées exposent :

```text
size_bytes
content_sha256
content_base64
payload_hash
```

La sérialisation JSON encode les octets en Base64. La désérialisation
revalide le Base64 et le hash du payload.

### `ArtifactPayloadVerificationPolicy`

La politique définit :

- le nombre maximal de payloads ;
- la taille maximale d’un payload ;
- la taille totale maximale ;
- la validation UTF-8 des types `text/*` ;
- les types de média exigeant une revue ;
- les types de média interdits ;
- les classifications exigeant une revue ;
- un hash SHA-256 déterministe.

### `ArtifactPayloadVerificationRequest`

La requête est liée cryptographiquement à :

- la politique ;
- l’identifiant du plan d’application ;
- le hash du plan d’application ;
- le hash du résultat de validation déclarative ;
- le plan, l’étape et l’agent ;
- l’orchestrateur demandeur ;
- l’horodatage UTC ;
- le nombre de payloads ;
- la taille totale ;
- le hash du manifeste complet des payloads.

`from_plan_and_payloads()` construit cette frontière depuis le plan, la
politique et les octets reçus.

L’ordre d’arrivée des payloads n’influence pas le hash du manifeste.

### `ArtifactPayloadVerificationRecord`

Chaque opération ou payload supplémentaire produit un enregistrement
comprenant :

- l’index déterministe ;
- la séquence d’opération éventuelle ;
- l’identifiant d’opération ;
- le chemin ;
- la décision ;
- la classification ;
- le SHA-256 attendu et observé ;
- la taille attendue et observée ;
- le type de média attendu et observé ;
- le hash du payload ;
- les raisons exactes.

### `ArtifactPayloadVerificationResult`

Le résultat contient :

- le statut global ;
- toutes les références de politique et de plan ;
- les payloads canoniquement ordonnés ;
- les enregistrements ;
- les raisons globales ;
- les compteurs ;
- la taille totale ;
- le hash du manifeste ;
- l’horodatage ;
- un hash SHA-256 du résultat complet.

Les octets restent présents dans le résultat afin que l’incrément
transactionnel suivant puisse les utiliser sans les relire depuis une source
non vérifiée.

## Décisions

### `verified`

Le résultat est `verified` uniquement lorsque :

- le plan d’application est `ready` ;
- le plan contient au moins une opération ;
- chaque opération possède exactement un payload ;
- aucun payload supplémentaire n’existe ;
- l’identifiant d’opération correspond ;
- le chemin correspond exactement ;
- le type de média correspond exactement ;
- la taille réelle correspond exactement ;
- le SHA-256 recalculé correspond exactement ;
- les limites de la politique sont respectées ;
- le contenu texte est UTF-8 lorsque la politique l’exige ;
- aucune règle de revue ou de rejet ne s’applique.

### `requires-review`

Une revue est requise lorsque les octets sont cohérents mais que la politique
classe le type de média ou la classification comme sensible.

Par défaut :

```text
application/octet-stream
```

exige une revue.

### `rejected`

Le résultat est refusé notamment pour :

- un plan non `ready` ;
- un plan sans opération ;
- un payload manquant ;
- un payload supplémentaire ;
- un `operation_id` inconnu ;
- plusieurs payloads pour la même opération ;
- plusieurs payloads visant la même destination portable ;
- un chemin différent ;
- un type de média différent ;
- une taille différente ;
- un SHA-256 différent ;
- un type de média interdit ;
- un texte non UTF-8 ;
- une limite de nombre ou de taille dépassée ;
- une frontière de requête incompatible.

## Ordre déterministe

Les payloads sont ordonnés par :

1. `operation_id` insensible à la casse ;
2. chemin insensible à la casse ;
3. chemin original ;
4. hash du payload.

Les enregistrements suivent d’abord l’ordre des opérations du plan. Les
payloads supplémentaires ou dupliqués suivent dans l’ordre canonique.

La même requête, le même plan, la même politique et les mêmes octets
produisent exactement le même JSON et les mêmes hashes.

## Validation de taille

La taille est calculée directement depuis les octets :

```text
actual_size_bytes = len(content)
```

Elle doit correspondre à `ArtifactApplicationOperation.size_bytes`.

La politique applique aussi :

- `max_payload_bytes` ;
- `max_total_bytes` ;
- `max_payloads`.

## Validation SHA-256

Le SHA-256 réel est recalculé directement :

```text
actual_sha256 = sha256(content)
```

Il doit correspondre à `ArtifactApplicationOperation.sha256`.

Le module ne fait confiance ni au hash d’origine ni à une taille annoncée
sans recalcul.

## Validation du type de média

Le type fourni par le payload doit correspondre exactement au type attendu
par l’opération.

Pour les types `text/*`, les octets doivent être décodables en UTF-8 lorsque
`validate_utf8_text` vaut `true`.

Le module ne tente pas encore de détecter automatiquement le véritable format
du contenu par signatures binaires ou analyse approfondie.

## Payloads manquants et supplémentaires

Chaque opération doit avoir exactement un payload.

Un payload dont le `operation_id` n’existe pas dans le plan est refusé.

Plusieurs payloads portant le même `operation_id` sont refusés, même lorsque
leurs octets sont identiques.

Les variantes de casse d’une même destination sont également considérées
comme conflictuelles pour préserver la portabilité Windows, macOS et Linux.

## Intégrité

Le module expose :

- `ArtifactPayload.payload_hash` ;
- `ArtifactPayloadVerificationPolicy.policy_hash` ;
- `ArtifactPayloadVerificationRequest.request_hash` ;
- `ArtifactPayloadVerificationResult.result_hash` ;
- un hash du manifeste des payloads ;
- le SHA-256 réel de chaque contenu.

Toute altération des octets, du Base64, d’un chemin, d’un média, d’un
enregistrement, d’un compteur ou du manifeste est détectée.

## Sérialisation

Les payloads, la politique, la requête et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

Les payloads, requêtes et résultats fournissent aussi `verify_hash()`.

Le JSON est compact, trié et déterministe.

## Garanties

- vérification des octets réels en mémoire ;
- recalcul exact de la taille ;
- recalcul exact du SHA-256 ;
- liaison stricte au plan d’application ;
- détection des payloads manquants et supplémentaires ;
- détection des doublons et conflits de destination ;
- ordre déterministe ;
- politique fail-closed ;
- absence de mutation des entrées ;
- aucune lecture du workspace ;
- aucune création de fichier ;
- aucune modification de fichier ;
- aucune suppression de fichier ;
- aucune création de sauvegarde ;
- aucune exécution de contenu ;
- aucune importation dynamique ;
- aucune connexion réseau ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- la détection automatique du format réel ;
- l’analyse antivirus ;
- l’analyse statique du code ;
- la détection de secrets dans les octets ;
- la comparaison avec une destination existante ;
- le verrouillage du workspace ;
- la création des sauvegardes ;
- l’écriture atomique ;
- l’exécution du rollback ;
- la reprise après panne ;
- l’intégration ELMAN Studio.
