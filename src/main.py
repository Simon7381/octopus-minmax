import threading

import config
from logger import setup_logging

logger = setup_logging()
logger.info("Octobot %s starting; DEBUG=%s; log file: logs/octobot.log", config.BOT_VERSION, config.DEBUG)

import web_server
from bot_orchestrator import BotOrchestrator

orchestrator = BotOrchestrator()
bot_thread = threading.Thread(target=orchestrator.start, daemon=False, name="BotThread")
web_thread = threading.Thread(
    target=web_server.run_server, daemon=False, name="WebThread"
)

# Start both threads
logger.info("Starting bot thread...")
bot_thread.start()

logger.info("Starting web server thread...")
web_thread.start()
bot_thread.join()
web_thread.join()
