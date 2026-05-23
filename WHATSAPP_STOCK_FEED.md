# WhatsApp Stock Feed for NandaEdge Advisor

This is the common reference document for the local WhatsApp sync workflow, scripts, folders, UI button, and Advisor endpoints.

The workflow is for this WhatsApp group:

```text
BuyAlertsContrbutingAndPaidMembers
```

It does not log in to WhatsApp, scrape WhatsApp Web, trade, or access broker accounts. It only reads a WhatsApp **Export Chat** zip or folder that you provide.

## Project Links

| Item | Link / Path | Purpose |
|---|---|---|
| Main app | `http://localhost:8765/` | NandaEdge Advisor dashboard |
| Day Trade page | `http://localhost:8765/?view=daytrade` | Day Trade workspace with WhatsApp Sync button |
| WhatsApp feed endpoint | `http://localhost:8765/whatsapp-stock-feed` | Reads the generated local feed JSON |
| WhatsApp sync endpoint | `http://localhost:8765/whatsapp-sync` | Scans Downloads once and refreshes the feed |
| Manual ingester | `scripts/whatsapp_stock_feed.py` | Reads one WhatsApp export zip/folder |
| Auto ingester | `scripts/whatsapp_stock_feed_auto.py` | Watches/scans Downloads for new export zips |
| Backend | `server.py` | Serves Advisor and local WhatsApp endpoints |
| Frontend | `index.html` | Shows the WhatsApp Sync button and status |
| Local output folder | `~/myprojects/edge-advisor-local/whatsapp-feeds/BuyAlertsContrbutingAndPaidMembers/` | Stores one-week local feed data |

## Current Design

```text
WhatsApp phone export
        ↓
~/Downloads/WhatsApp Chat - BuyAlertsContrbutingAndPaidMembers.zip
        ↓
scripts/whatsapp_stock_feed_auto.py --once
        ↓
scripts/whatsapp_stock_feed.py
        ↓
~/myprojects/edge-advisor-local/whatsapp-feeds/BuyAlertsContrbutingAndPaidMembers/whatsapp_stock_feed.json
        ↓
http://localhost:8765/whatsapp-stock-feed
        ↓
NandaEdge Advisor WhatsApp Sync status
```

## Export the Group

On your phone:

1. Open WhatsApp.
2. Open `BuyAlertsContrbutingAndPaidMembers`.
3. Tap the group name.
4. Choose **Export Chat**.
5. Choose **Include Media** if you want images/videos/documents copied.
6. Save or AirDrop the `.zip` file to your Mac.

Expected default location:

```text
~/Downloads/
```

## Manual Run

Use this when you know the exact export file path.

```bash
cd /Users/rajamac/myprojects/edge-advisor
python3 scripts/whatsapp_stock_feed.py \
  --input "/path/to/WhatsApp Chat - BuyAlertsContrbutingAndPaidMembers.zip"
```

Useful help:

```bash
python3 scripts/whatsapp_stock_feed.py --help
```

## Auto Mode

WhatsApp does not provide a safe personal-chat API for direct background downloads. The supported local automation is:

1. Keep this watcher running.
2. Export the group chat to `~/Downloads`.
3. The watcher auto-detects the new WhatsApp export zip and feeds it into Advisor.

```bash
cd /Users/rajamac/myprojects/edge-advisor
python3 scripts/whatsapp_stock_feed_auto.py
```

To scan once and exit:

```bash
python3 scripts/whatsapp_stock_feed_auto.py --once
```

To watch a custom folder:

```bash
python3 scripts/whatsapp_stock_feed_auto.py \
  --watch "/Users/rajamac/Downloads"
```

Useful help:

```bash
python3 scripts/whatsapp_stock_feed_auto.py --help
```

## Main Page Button

The main Advisor page has a **WhatsApp Sync** button beside the Overview / Day Trade tabs.

Open:

```text
http://localhost:8765/?view=daytrade
```

Then click:

```text
WhatsApp Sync
```

What the button does:

1. Calls `http://localhost:8765/whatsapp-sync`.
2. Runs `scripts/whatsapp_stock_feed_auto.py --once`.
3. Looks for a matching WhatsApp export zip in `~/Downloads`.
4. Updates the local feed JSON if a new export exists.
5. Updates the on-screen status.

The button does not access WhatsApp directly. It only processes local exported files.

## Output Files

Default output folder:

```text
~/myprojects/edge-advisor-local/whatsapp-feeds/BuyAlertsContrbutingAndPaidMembers/
```

The scripts write:

```text
whatsapp_stock_feed.json
media/images/
media/videos/
media/documents/
text/messages_last_7_days.txt
```

## Script Details

### `scripts/whatsapp_stock_feed.py`

Purpose:

- Reads one WhatsApp export zip or extracted folder.
- Parses chat text.
- Copies images, videos, and documents into local cache folders.
- Extracts possible stock symbols, bias, price levels, sample messages, confidence, and reasons.
- Writes `whatsapp_stock_feed.json`.
- Prunes local copied files older than the retention period.

Example:

```bash
python3 scripts/whatsapp_stock_feed.py \
  --input "/path/to/export.zip" \
  --retention-days 7
```

### `scripts/whatsapp_stock_feed_auto.py`

Purpose:

- Watches or scans a local folder, normally `~/Downloads`.
- Finds WhatsApp export zips for `BuyAlertsContrbutingAndPaidMembers`.
- Waits for the zip file to finish downloading.
- Runs `scripts/whatsapp_stock_feed.py` automatically.
- Tracks already-processed exports so the same zip is not reprocessed.

Example:

```bash
python3 scripts/whatsapp_stock_feed_auto.py --once
```

## Server Endpoints

### `/whatsapp-stock-feed`

Reads the latest local JSON feed:

```bash
curl 'http://localhost:8765/whatsapp-stock-feed'
```

If no export has been processed yet, it returns a friendly message and the expected local path.

### `/whatsapp-sync`

Runs the safe local scan once:

```bash
curl 'http://localhost:8765/whatsapp-sync'
```

Expected response when no export exists yet:

```text
no new matching WhatsApp export found
```

## Custom Feed Path

If you use a custom JSON path:

```bash
WHATSAPP_STOCK_FEED_FILE="/custom/path/whatsapp_stock_feed.json" python3 server.py
```

## Retention

The script keeps copied media/text for 7 days by default and prunes older files on each run:

```bash
python3 scripts/whatsapp_stock_feed.py \
  --input "/path/to/export.zip" \
  --retention-days 7
```

Run it on Friday/weekend to clean old files, or schedule it with a local automation.

## Local Server

Run NandaEdge Advisor locally:

```bash
cd /Users/rajamac/myprojects/edge-advisor
HOST=localhost PYTHONPYCACHEPREFIX=/private/tmp/codex_pycache python3 server.py
```

Open:

```text
http://localhost:8765/
```

## Decision Use

Treat the WhatsApp feed as read-only sentiment and alert intelligence. NandaEdge Advisor should validate every symbol and level against live market data, candles, technicals, and options context before any manual decision.

No live trading, order placement, broker login, or account access is part of this workflow.
