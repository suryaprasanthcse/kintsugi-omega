from scout.util.retry import retry_with_backoff

CORPUS = {"doc-1": "rate limits", "doc-2": "retries"}


class ScratchBuffer:
    """Per-attempt partial results. Resetting it on retry is intended (spec §5.9.2)."""

    def __init__(self):
        self.hits = []

    def add(self, doc_id):
        self.hits.append(doc_id)


def fetch_document(doc_id):
    return CORPUS[doc_id]


def answer(question):
    def attempt():
        buffer = ScratchBuffer()
        for doc_id in CORPUS:
            if question in fetch_document(doc_id):
                buffer.add(doc_id)
        return buffer.hits

    return retry_with_backoff(attempt)
