---
name: market-data-debug
description: "Use when market data feed issues are suspected. Triggers on 'prices are wrong', 'feed is down', 'data looks weird', 'missing ticks', 'stale prices', 'why did the strategy do X', or any data quality problem. Systematic diagnostic for Chainlink, Vatic, Gamma, and CLOB feeds."
---

# Market Data Debug

Quand les signaux ou le P&L semblent faux, le probleme est presque toujours dans les donnees, pas dans la logique de strategie. Cette skill fournit une procedure de diagnostic systematique.

## Sequence de diagnostic

Executer dans l'ordre. Ne pas sauter au "fix" avant d'avoir complete le diagnostic.

### Phase 1: Feed Health Check

Lancer `python scripts/health_check.py` et capturer la sortie. Verifier:

1. **Chainlink WebSocket**: Connexion ouverte ? Dernier prix recu quand ? Prix raisonnable ?
2. **Vatic API**: Repond ? Strike price de l'intervalle courant present ? Derniere MAJ ?
3. **Gamma API**: Marches actifs decouverts ? Token IDs coherents ?
4. **CLOB API**: Orderbook peuple ? Spread bid/ask raisonnable ?

### Phase 2: Data Freshness

Pour chaque feed:
- Timestamp du dernier message recu vs temps courant
- Si gap > frequence attendue (1s Chainlink, 30s Vatic) -> flag STALE
- Checker la sortie de `data_recorder` pour les gaps (timestamps manquants)

### Phase 3: Cross-Feed Consistency

Comparer les prix entre feeds au meme timestamp:
- Prix Chainlink vs CLOB mid-price: doit etre dans ~0.5% en conditions normales
- Si divergence > 1%: soit un feed est stale, soit il y a un vrai arb
- Verifier que le strike price Vatic est entre le prix Chainlink et le CLOB mid

### Phase 4: Historical Pattern

Lire les donnees recentes de `data/raw/`:
- Tabuler la serie de prix sur les 30 dernieres minutes
- Identifier: gaps, sauts, periodes plates (stale), doublons
- Comparer contre des evenements de marche connus

### Phase 5: Root Cause Classification

| Symptome | Cause probable | Fix |
|----------|---------------|-----|
| Aucun prix | WebSocket deconnecte | Verifier logique reconnexion, reseau |
| Prix stop updating | Feed stale, probleme serveur | Reconnexion; si persiste, checker status Chainlink |
| Prix erratiques | Feed delivre out-of-order | Verifier tri timestamps dans aggregator |
| CLOB et Chainlink divergent > 2% | Orderbook stale OU vrai arb | Checker connexion CLOB; si sain, la strategie arb devrait tirer |
| Strategie fire mais pas de fills | Orderbook vide ou spread trop large | Checker liquidite marche; pause si necessaire |
| Prix dupliques (meme timestamp) | Feed delivre des doublons | Ajouter dedup dans aggregator |

## Output

```
## Data Debug Report -- [Date] [Time]

### Feed Status
| Feed | Status | Last Update | Staleness |
|------|--------|-------------|-----------|
| Chainlink WS | ... | ... | ... |
| Vatic API | ... | ... | ... |
| Gamma API | ... | ... | ... |
| CLOB API | ... | ... | ... |

### Cross-Feed Consistency
- Chainlink vs CLOB mid: [divergence]%
- Assessment: [CONSISTENT / DIVERGENT / ONE_STALE]

### Root Cause
[Classification du tableau ci-dessus]

### Recommended Action
[Fix specifique ou etape d'investigation]
```
