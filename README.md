# XAUUSD / Gold News Bot

Hlida ekonomicke zpravy ovlivnujici **zlato (XAUUSD)** a posila je na Discord.

Bezi v **GitHub Actions** - tedy v cloudu. **Nepotrebuje zapnuty pocitac.**

## Co dela

1. **Denni souhrn v 07:00 (Praha)** - vypis vsech dnesnich **High** a **Medium impact**
   USD zprav. Kdyz zadne nejsou, posle zelenou zpravu "klidny den".
2. **Upozorneni 5 minut pred kazdou zpravou** - zvlast pro kazdy event.

## Zdroje dat

| Poradi | Zdroj | Poznamka |
|---|---|---|
| 1. | Forex Factory (weekly JSON feed) | primarni; feed rate-limituje, proto povinna cache 1 h |
| 2. | TradingView economic calendar | automaticka zaloha, kdyz je FF nedostupny |

Filtr: mena `USD` + globalni eventy `All`, impact `High` nebo `Medium`.

## Jak to bezi

- `.github/workflows/goldnews.yml` - cron `*/5 * * * *` (nejkratsi interval na GitHubu).
- Pro **presnost na minutu** job pri detekci blizici se zpravy *dospi* presne
  na T-5:00 a teprve pak posle ping.
- `state.json` se commituje zpet do repa - proto se zadna zprava neposle dvakrat,
  i kdyz kazdy beh startuje v cistem VM.
- `ff_cache.json` se take commituje - jinak by kazdy beh dotazoval Forex Factory
  a okamzite narazil na rate-limit (HTTP 429).

## Rezimy

```bash
python goldnews.py --watch      # hlidaci beh (tohle dela cron)
python goldnews.py --digest     # jen denni souhrn
python goldnews.py --now        # okamzity souhrn na vyzadani
python goldnews.py --selftest   # 25 internich testu
python goldnews.py --watch --dry  # nic neposila, jen vypise
```

## Nastaveni

Webhook je v **GitHub secretu** `DISCORD_WEBHOOK` (Settings -> Secrets and
variables -> Actions). V repu neni ulozeny.

Lokalne se bere z `DISCORD_WEBHOOK` nebo z `config.json` (ten je v `.gitignore`).

## Vypnuti / zapnuti

Actions -> "GoldNews Watcher" -> `...` -> Disable / Enable workflow.
