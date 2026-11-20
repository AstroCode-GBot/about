VENZOO PREMIUM EMOJI BOT — RENDER SETUP

FILES
- app.py: Telegram bot + Flask web/health endpoint
- requirements.txt: Python dependencies
- render.yaml: Render Blueprint config
- data/: JSON files are created automatically when the bot runs

RENDER DEPLOY
1. Upload these files to a GitHub repository.
2. In Render, choose New -> Blueprint and connect that repository, or create a Python Web Service.
3. Build command: pip install -r requirements.txt
4. Start command: gunicorn --workers 1 --threads 4 --timeout 120 app:app
5. Add environment variables:
   BOT_TOKEN = your Telegram BotFather token
   ADMIN_ID = 7932057377
   BOT_USERNAME = VenzooPremiumEmojiBot
   DATA_DIR = data
6. Deploy. Open the Render URL. It should show:
   @VenzooPremiumEmojiBot is running
7. Use the root URL in UptimeRobot. The /health URL is also available for monitoring.

IMPORTANT TELEGRAM SETUP
- Make @VenzooPremiumEmojiBot an administrator in each destination channel.
- Give it Post Messages permission. Also enable Edit Messages and Delete Messages if those options are available.
- Users connect channels privately by forwarding a channel post or sending @channelusername/channel ID.
- The bot can only manage messages it has permission to manage. Telegram can reject edits/deletions for messages that are too old or not owned by the bot.

DATA STORAGE
- No SQL database is used. User/channel/post records are JSON files in DATA_DIR.
- Render's default filesystem is ephemeral. JSON files can be lost when the service is redeployed/replaced.
- For persistent JSON storage, attach a persistent disk in Render and set DATA_DIR to its mount path (for example /var/data). Keep one service instance/worker because this bot uses Telegram long polling.

SECURITY
- Never publish your BOT_TOKEN in source code or screenshots.
- If the token was exposed, regenerate it with @BotFather and update Render's BOT_TOKEN environment variable.

FREE RENDER STORAGE WARNING
- Render free services do not include a persistent disk. Their local files can be lost when the service restarts, redeploys, or spins down.
- Since this bot stores user/channel/post data in JSON (as requested), the free plan is suitable for testing but is not durable storage.
- Render documents persistent disks as paid-service storage. If you need reliable retention, attach a disk and set DATA_DIR to the disk mount path (for example /var/data).
