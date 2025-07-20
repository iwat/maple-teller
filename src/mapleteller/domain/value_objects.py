from dataclasses import dataclass
from datetime import date


@dataclass
class Transaction:
    tx_date: date
    post_date: date
    payee: str
    credit: int | None  # in cents
    debit: int | None  # in cents
    balance: int | None  # in cents
    note: str | None

    def __post_init__(self):
        assert (self.credit is None) != (self.debit is None), f'Invalid transaction {self}'
        if self.credit is not None:
            assert self.credit >= 0, f'Invalid transaction {self}'
        if self.debit is not None:
            assert self.debit >= 0, f'Invalid transaction {self}'
