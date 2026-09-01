# XAUUSD / Gold News Bot

Hlida ekonomicke zpravy ovlivnujici **zlato (XAUUSD)** a posila je na Discord.

Bezi v **GitHub Actions** - v cloudu. **Nepotrebuje zapnuty pocitac.**

## Co dela

1. **Denni souhrn v 07:00 (Europe/Prague)** - vsechny dnesni **High** a
   **Medium impact** USD zpravy. Kdyz zadne nejsou, posle zelenou zpravu.
2. **Upozorneni presne 5 minut pred zpravou.** Zpravy se stejnym casem
   (typicky 16:00) se sloucí do **jedne** zpravy, ne N samostatnych.

## Architektura

GitHub `schedule` (cron) umi nejcasteji `*/5` a **byva zpozdeny nebo se pri
zatezi preskoci** - na presnost "5 minut predem" se na nej nelze spolehnout.

Proto je hlavni motor **dlouhobezici smycka** s kontrolou kazdych **45 s**:

| Workflow | Trigger | Role |
|---|---|---|
| `loop.yml` (Loop A) | `workflow_run` po Loop B, `workflow_dispatch`, `*/30` cron | presny hlidac, ~5h20m |
| `loop_b.yml` (Loop B) | `workflow_run` po Loop A, `workflow_dispatch` | presny hlidac, ~5h20m |
| `goldnews.yml` (Watcher) | `*/5` cron, `workflow_dispatch` | zaloha; **sama se vypne**, kdyz smycka bezi |

A a B se stridaji pres udalost `workflow_run` -> **nekonecny beh bez zavislosti
na cronu**. Protoze `workflow_run` se spusti po *jakemkoli* dokonceni (vcetne
`cancelled` a `failure`), system se **sam zotavi z padu**.

Pojistky v `Guard` kroku:
- nespusti se druha smycka, kdyz uz jedna bezi (kontrola pres `gh run list`),
- nezretezi se po behu kratsim nez 120 s (jinak by A a B po sobe strilely
  v nekonecne smycce no-op behu).

## Zdroje dat

| Poradi | Zdroj | Poznamka |
|---|---|---|
| 1. | Forex Factory (weekly JSON feed) | primarni; rate-limituje (HTTP 429) -> povinna cache 1 h |
| 2. | TradingView economic calendar | automaticka zaloha |

Kdyz je FF nedostupny a cache starsi nez **20 h**, bot prepne na TradingView -
nikdy neposila zastarala data. Kdyz padnou oba, pouzije starou cache a
oznaci to ve zprave.

Filtr: mena `USD` + globalni eventy `All`, impact `High` nebo `Medium`.

## Proc nic neprijde dvakrat

`state.json` (co uz bylo odeslano) se **commituje zpet do repa**, takze pamet
prezije restart stroje. `ff_cache.json` se commituje take - jinak by kazdy beh
dotazoval Forex Factory a okamzite narazil na rate-limit.

## Rezimy

```bash
python goldnews.py --loop --minutes 320 --interval 45   # presna smycka (cloud)
python goldnews.py --watch                              # jeden pruchod (zaloha)
python goldnews.py --digest                             # jen denni souhrn
python goldnews.py --now                                # souhrn na vyzadani
python goldnews.py --selftest                           # 37 internich testu
python goldnews.py --watch --dry --no-sleep              # nic neposila
```

## Nastaveni

Webhook je v GitHub secretu `DISCORD_WEBHOOK` (Settings -> Secrets and
variables -> Actions). V repu ulozeny neni.
Lokalne se bere z env `DISCORD_WEBHOOK` nebo z `config.json` (v `.gitignore`).
