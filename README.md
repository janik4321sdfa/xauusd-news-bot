# XAUUSD / Gold News Bot

Hlida ekonomicke zpravy ovlivnujici **zlato (XAUUSD)** a posila je na Discord.
Bezi v **GitHub Actions**. **Nepotrebuje zapnuty pocitac.**

## Co posila

**Denni souhrn v 07:00 (Europe/Prague)** a **upozorneni presne 5 minut pred
kazdou zpravou**. Zpravy se stejnym casem se sloucí do jedne zpravy.

Kazda zprava je karta:

```
🔴  16:00  ·  ISM Manufacturing PMI
čeká se **55.2**  ·  minule 55.6
📈 pod **55.2** → zlato ROSTE
📉 nad **55.2** → zlato PADÁ
```

Zobrazuje se **vyhradne reakcni pravidlo** (od jakeho cisla se to lame).
Dohad o smeru z prognozy se zamerne NEzobrazuje - pred vydanim se smer urcit
neda, trh reaguje na odchylku skutecneho cisla od prognozy.

Polarita ukazatele se prehazuje automaticky:
- vetsina dat: vyssi cislo = silnejsi ekonomika = zlato pada
- nezamestnanost / zadosti o podporu: vyssi cislo = slabsi ekonomika = zlato roste

Bez prognozy (reci centralnich bankeru, jednani, aukce) se vypise
`bez prognózy — dopad na zlato nejasný`.

## Architektura

GitHub `schedule` (cron) u tohoto repa **nesepnul ani jednou**, takze bot na nem
nebezi. Hlavni motor je **dlouhobezici smycka** s kontrolou kazdych **45 s**
(odtud presnost na minutu). Jeden beh ~5h20m.

| Workflow | Trigger | Role |
|---|---|---|
| `loop.yml` (Loop A) | `workflow_dispatch`, `workflow_run` po B, `*/30` cron | presny hlidac |
| `loop_b.yml` (Loop B) | `workflow_dispatch`, `workflow_run` po A | presny hlidac |
| `goldnews.yml` (Watcher) | `*/5` cron, `workflow_dispatch` | zaloha; sama se vypne, kdyz smycka bezi |

### Jak se to restartuje

Posledni krok kazde smycky (`if: always()`, tedy i pri padu nebo zruseni)
nastartuje **druhou** smycku pres `gh workflow run` s tokenem `SELF_TOKEN`.

**Proc PAT a ne `GITHUB_TOKEN`:** udalosti vyvolane `GITHUB_TOKEN`em GitHub
zamerne nespousti (ochrana proti rekurzi), takze self-dispatch by neudelal nic.

**Proc nestaci `workflow_run`:** GitHub omezuje zanoreni `workflow_run` na
~3 urovne. Retez `dispatch -> B -> A -> B` se na ctvrtem skoku uz nespusti -
overeno v praxi. `workflow_dispatch` pres PAT zanoreni resetuje, proto je
primarni cestou. `workflow_run` zustava jako zaloha.

### Ochrany v kroku `Guard`

- **Jen jedna smycka zaraz.** Kontrola pres `gh run list` nad obema workflow.
- **Cekani na predchudce.** Pri predavani rizeni predchozi beh jeste dobiha;
  guard na nej ceka az ~100 s, jinak by se nova smycka vypnula a hlidani skoncilo.
- **Zadny ping-pong.** Po behu kratsim nez 120 s se nezretezuje (to byl "skip",
  ne skutecna prace) - bez toho by A a B po sobe strilely v nekonecne smycce.

## Zdroje dat

| Poradi | Zdroj | Poznamka |
|---|---|---|
| 1. | Forex Factory (weekly JSON) | primarni; rate-limituje (429) -> cache min. 1 h |
| 2. | TradingView economic calendar | automaticka zaloha |

Kdyz je FF nedostupny a cache starsi nez **20 h**, prepne se na TradingView -
nikdy neposila zastarala data. Kdyz padnou oba, pouzije starou cache a oznaci to.

Filtr: mena `USD` + globalni eventy `All`, impact `High` / `Medium`.

## Proc nic neprijde dvakrat

`state.json` (odeslane zpravy) i `ff_cache.json` se **commituji zpet do repa** -
kazdy beh startuje v cistem VM, takze bez toho by pamet nedrzela a FF by okamzite
narazil na rate-limit.

## Rezimy

```bash
python goldnews.py --loop --minutes 320 --interval 45   # presna smycka (cloud)
python goldnews.py --watch                              # jeden pruchod (zaloha)
python goldnews.py --digest                             # jen denni souhrn
python goldnews.py --now                                # souhrn na vyzadani
python goldnews.py --selftest                           # 100 internich testu
python preview.py                                       # jak zpravy vypadaji
```

## Secrety

| Nazev | K cemu |
|---|---|
| `DISCORD_WEBHOOK` | kam posilat zpravy |
| `SELF_TOKEN` | nastartovani navazne smycky (PAT se scope `workflow`) |

Obnoveni tokenu: lokalne `python set_self_token.py` (cte ho z Windows
Credential Manageru).
