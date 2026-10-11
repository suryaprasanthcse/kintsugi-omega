"""Scout: answers research questions from a frozen document corpus, with citations.

Spec §5.9.2. ``web_search`` is BM25 over a frozen local corpus (no internet variance);
``fetch_document`` reads a document page by page from a document store behind the chaos proxy;
the model composes the answer from the fetched passages and may ask for one follow-up search;
``verify_citations`` keeps only citations of passages Scout actually read.
"""
