RATES = {"USD": 1.0, "EUR": 1.08}


def fx_rate(currency):
    return RATES[currency]


def post_journal_entry(db, line):
    """Side-effecting tool: appends a journal line to the ledger."""
    db.append(line)
