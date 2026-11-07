# Udemy Deal Alert

Checks public Udemy coupon feeds every 5 minutes (GitHub Actions; GitHub may delay scheduled runs) and sends new
matching deals to Telegram. Free, no server, no Udemy login. New alerts are sent only when the Udemy coupon page provides clear evidence of a zero price; unclear checks are retried rather than advertised as free. Previously discovered coupon links are also rechecked in rotating batches (25 per run by default).

## Setup
1. Create a Telegram bot with @BotFather and keep the token private.
2. Get your chat id (send a message to the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`).
3. In the repo: Settings > Secrets and variables > Actions > New repository secret:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Actions tab > "Udemy Deal Check" > Run workflow.

## Configure
Edit `config.json`:
- `min_discount`: 100 = free only, 80 = 80% and above
- `categories`: keywords matched against title, category and description
- `exclude_keywords`: skip deals containing these words
- `min_rating`: only applied when the source provides a rating
- `max_verifies_per_run`: maximum previously discovered coupons rechecked each run (default: 25)

## Local commands
```
python check_deals.py --test   # send a test Telegram message
python check_deals.py --dry    # show matches without sending anything
```
Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as environment variables for `--test`.
