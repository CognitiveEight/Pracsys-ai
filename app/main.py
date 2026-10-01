"""
Main application entry point.
"""

import asyncio
import signal

from loguru import logger

from app.core.config import settings
from app.core.lifecycle import lifecycle
from app.observability.logger import setup_logging


class Application:
    """Main application class."""

    def __init__(self):
        self._shutdown_event = asyncio.Event()
        self._setup_signal_handlers()

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        logger.info("Received shutdown signal: {}", signal.Signals(signum).name)
        self._shutdown_event.set()

    async def run(self) -> None:
        """Run the application."""
        try:
            logger.info(
                "Starting {} - {}", settings.app_name.upper(), settings.app_env.upper()
            )
            # Start application
            await lifecycle.startup()

            logger.info("Application is running. Press Ctrl+C to stop.")

            # Wait for shutdown signal
            await self._shutdown_event.wait()

        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        except Exception as e:
            logger.error("Application error: {}", str(e))
            raise
        finally:
            logger.info("Shutting down application...")
            await lifecycle.shutdown()
            logger.info("Application stopped")
async def main() -> None:
    """Main entry point."""
    app = Application()
    await app.run()


if __name__ == "__main__":
    # Setup logging first
    setup_logging()

    # Run application
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical("Fatal error: {}", str(e))
        raise
