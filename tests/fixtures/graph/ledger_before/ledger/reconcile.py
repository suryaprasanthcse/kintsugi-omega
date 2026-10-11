from ledger.tools import fx_rate, post_journal_entry
from ledger.util.retry import retry_with_backoff


class ReconcileSession:
    def __init__(self, batch):
        self.batch = batch
        self.posted = set()

    def post(self, db, line):
        if line["key"] in self.posted:
            return
        post_journal_entry(db, line)
        self.posted.add(line["key"])


def run_reconcile(db, batch):
    def attempt():
        session = ReconcileSession(batch)
        for line in batch:
            rate = fx_rate(line["currency"])
            session.post(db, {**line, "rate": rate})
        return session

    return retry_with_backoff(attempt)
