import logging
import socket
import time
import uuid

from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal
from app.services.durable_jobs import claim_next_job
from app.services.job_execution import execute_claimed_job

logger = logging.getLogger("payment_orchestrator.worker")
POLL_INTERVAL_SECONDS = 1


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    worker_id = f"{socket.gethostname()}-{uuid.uuid4()}"
    logger.info("Durable job worker started: %s", worker_id)
    while True:
        try:
            job_id = claim_next_job(SessionLocal, worker_id)
            if job_id is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            execute_claimed_job(SessionLocal, job_id, worker_id)
        except SQLAlchemyError:
            logger.exception("Worker database operation failed")
            time.sleep(POLL_INTERVAL_SECONDS)
        except Exception:
            logger.exception("Worker iteration failed")
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
