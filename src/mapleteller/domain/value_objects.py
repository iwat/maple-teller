from dataclasses import dataclass
from datetime import date


@dataclass
class Transaction:
    transaction_date: date
    post_date: date
    payee: str
    credit: int | None  # in cents
    debit: int | None  # in cents
    balance: int | None  # in cents
    note: str

    def __post_init__(self):
        assert (self.credit is None) != (self.debit is None), f'Invalid transaction {self}'
