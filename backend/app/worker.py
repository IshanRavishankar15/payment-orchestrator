import logging
import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import engine

logger = logging.getLogger("payment_orchestrator.worker")
POLL_INTERVAL_SECONDS = 30


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("Worker started; durable job processing is introduced in a later phase")
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            logger.debug("Database connection is healthy")
        except SQLAlchemyError:
            logger.exception("Worker database health check failed")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
