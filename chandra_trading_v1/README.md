# Chandra Trading V1 - Dual Strategy

V1 supports running either or both Chandra strategies at the same time:

- **Strategic Entry** — the supplied `buy_sell` execution mode (BuyCondition/SellCondition followed by Strategic confirmation).
- **Magical Entry** — the supplied `magical` execution mode (new Magical BUY/SELL events).

Each strategy has its own execution state, MT5 magic number, position, initial/trailing L100 stop, exits, and trade log. The dashboard also shows combined P&L.

## Equiti

For the tested Equiti account, the display symbol is `XAUUSD` and the MT5 symbol resolves to `XAUUSD.sd`.

## Paper / Live

Paper mode uses the live MT5 market feed but does not send broker orders.

Live mode sends real MT5 orders. Both strategies are isolated by MT5 magic number:

- Strategic Entry: `26081201`
- Magical Entry: `26081202`

Do not enable Live mode until Paper mode has been validated.

## Start

```powershell
.venv\Scripts\Activate.ps1
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`.

## Dependencies

```powershell
pip install -r backend\requirements.txt
```
