# WhatsApp Stock Feed for NandaEdge Advisor

This is a local-only workflow for the WhatsApp group:

```text
BuyAlertsContrbutingAndPaidMembers
```

It does not log in to WhatsApp, scrape WhatsApp Web, trade, or access broker accounts. It only reads a WhatsApp **Export Chat** zip or folder that you provide.

## Export the Group

On your phone:

1. Open WhatsApp.
2. Open `BuyAlertsContrbutingAndPaidMembers`.
3. Tap the group name.
4. Choose **Export Chat**.
5. Choose **Include Media** if you want images/videos/documents copied.
6. Save or AirDrop the `.zip` file to your Mac.

## Run Locally

From the project folder:

```bash
cd /Users/rajamac/myprojects/edge-advisor
python3 scripts/whatsapp_stock_feed.py \
  --input "/path/to/WhatsApp Chat - BuyAlertsContrbutingAndPaidMembers.zip"
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

Default output:

```text
~/myprojects/edge-advisor-local/whatsapp-feeds/BuyAlertsContrbutingAndPaidMembers/
```

The script writes:

```text
whatsapp_stock_feed.json
media/images/
media/videos/
media/documents/
text/messages_last_7_days.txt
```

## Advisor Endpoint

When the local server is running, NandaEdge Advisor can read the generated feed here:

```text
http://localhost:8765/whatsapp-stock-feed
```

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

## Decision Use

Treat the WhatsApp feed as read-only sentiment and alert intelligence. NandaEdge Advisor should validate every symbol and level against live market data, candles, technicals, and options context before any manual decision.
