"""Ledger: turns vendor documents (invoices, credit notes, payment advices) into journal lines.

Spec §5.9.1. The model reads the document (``parse_invoice``); everything after that is code:
``lookup_vendor`` against the vendor master, conversion to USD with ``fx_rate`` (an external FX
API behind the chaos proxy), and ``post_journal_entry``, which writes to the ledger database and
is the host's side-effecting tool.
"""
