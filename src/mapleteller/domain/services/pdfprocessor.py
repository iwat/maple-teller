import logging
import re
import sys
from abc import ABC, abstractmethod
from datetime import date
from typing import Self

import pdfplumber
import pdfplumber.page
from mapleteller.domain import Transaction


MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']


def sanitize_string(s: str) -> str:
    while '  ' in s:
        s = s.replace('  ', ' ')
    return s


def sanitize_amount(s: str) -> str:
    return s.replace(' ', '').replace(',', '').replace('.', '').replace('$', '').strip()


class PDFProcessor(ABC):
    @staticmethod
    def process(file: str, logger: logging.Logger) -> list[Transaction]:
        logger.info('Processing file %s', file)
        with pdfplumber.open(file) as pdf:
            with pdfplumber.open(file) as pdf:
                first_page = pdf.pages[0].extract_text(layout=True, x_tolerance=1)

                processor = None
                for processor_cls in [
                    OldBMOMastercardPDFProcessor,
                    BMOMastercardPDFProcessor,
                    BMOChequingPDFProcessor,
                    RBCChequingPDFProcessor,
                    RBCMastercardPDFProcessor,
                ]:
                    processor = processor_cls.try_create_processor(first_page, logger)
                    if processor is not None:
                        break

                if processor is None:
                    print(first_page, file=sys.stderr)
                    raise ValueError('No idea what this is')

                try:
                    processor.process_first_page(first_page)
                except Exception:
                    print(first_page, file=sys.stderr)
                    raise

                transactions = []
                with pdfplumber.open(file) as pdf:
                    for page in pdf.pages:
                        text = processor.extract_text(page)
                        logger.info('Processing page %d', page.page_number)
                        transactions.extend(processor.process_page(text))

                return processor.post_process_transactions(transactions)

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def process_page(self, text: str) -> list[Transaction]:
        lines = text.split('\n')
        transactions = []

        began_transaction = False
        for i in range(len(lines)):
            self.logger.debug('[?] %s', lines[i])
            if self.should_begin_processing_transaction(lines[i]):
                lines = lines[i + 1:]
                began_transaction = True
                break

        if not began_transaction:
            self.logger.warning('No transaction found on current page')
            return []

        for i in range(len(lines)):
            self.logger.debug('[_] %s', lines[i])

            if self.should_stop_processing_doc(lines[i]):
                self.logger.info('Document processing stopped')
                break
            if self.should_stop_processing_page(lines[i]):
                self.logger.info('Page processing stopped')
                break

            if i == 0:
                prev_line = None
            else:
                prev_line = lines[i - 1]

            if i + 1 < len(lines) and not self.should_stop_processing_page(lines[i + 1]):
                next_line = lines[i + 1]
            else:
                next_line = None

            tx = self.prepare_new_transaction(lines[i], prev_line, next_line)
            if tx:
                self.logger.info('[T] %s', tx)
                transactions.append(tx)

        return transactions

    @abstractmethod
    def extract_text(self, page: pdfplumber.page.Page) -> str: ...

    @abstractmethod
    def process_first_page(self, text: str) -> None: ...

    @abstractmethod
    def should_begin_processing_transaction(self, line: str) -> bool: ...

    @abstractmethod
    def prepare_new_transaction(self, line: str, prev_line: str | None, next_line: str | None) -> Transaction | None: ...

    @abstractmethod
    def should_stop_processing_page(self, line: str) -> bool: ...

    @abstractmethod
    def should_stop_processing_doc(self, line: str) -> bool: ...

    @abstractmethod
    def post_process_transactions(self, transactions: list[Transaction]) -> list[Transaction]: ...

class BMOChequingPDFProcessor(PDFProcessor):
    DESC_SLICE = slice(None, 68)
    CREDIT_SLICE = slice(70, 87)
    DEBIT_SLICE = slice(90, 107)
    BALANCE_SLICE = slice(109, 125)

    TX_PATTERN = re.compile(r'\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(.+)')

    @classmethod
    def try_create_processor(cls, first_page: str, logger: logging.Logger) -> Self | None:
        if 'BMO' in first_page:
            logger.debug('Looks like a BMO statement')
            if 'Summary of your account' in first_page:
                logger.debug('Looks like a BMO Banking Account statement')
                return cls(logger)
        elif 'Summary of your account' in first_page:
            logger.debug('Looks like a BMO Banking Account statement')
            return cls(logger)
        return None

    def __init__(self, logger: logging.Logger):
        super().__init__(logger)
        self.year = None
        self.closing_debit = None
        self.closing_cretit = None

    def extract_text(self, page: pdfplumber.page.Page) -> str:
        return page.extract_text(layout=True, x_density=4.5, x_tolerance=1)

    def process_first_page(self, text: str) -> None:
        for line in text.split('\n'):
            m = re.fullmatch(r'\s+For\s+the\s+period\s+ending\s+[A-Z][a-z]+\s+\d{2},\s+(\d{4}).*', line)
            if m:
                self.logger.info('Year found: %s', m.group(1))
                self.year = int(m.group(1))
                break

        assert self.year is not None, 'Year not found'

    def should_begin_processing_transaction(self, line: str) -> bool:
        return re.fullmatch(r'\s+Date\s+Description\s+.*', line) is not None

    def prepare_new_transaction(self, line: str, prev_line: str | None, next_line: str | None) -> Transaction | None:
        desc = line[BMOChequingPDFProcessor.DESC_SLICE]
        credit = line[BMOChequingPDFProcessor.CREDIT_SLICE]
        debit = line[BMOChequingPDFProcessor.DEBIT_SLICE]
        balance = line[BMOChequingPDFProcessor.BALANCE_SLICE]

        self.logger.debug('d: [%s]', desc)
        self.logger.debug('c: [%s]', credit)
        self.logger.debug('d: [%s]', debit)
        self.logger.debug('b: [%s]', balance)

        tx_matcher = BMOChequingPDFProcessor.TX_PATTERN.fullmatch(desc)
        if tx_matcher is None:
            return None

        self.logger.debug('tx_matcher: %s', tx_matcher.groups())

        note = tx_matcher.group(3).strip()

        if 'Opening balance' in note:
            return None

        if next_line:
            next_desc = next_line[BMOChequingPDFProcessor.DESC_SLICE]
            next_credit = next_line[BMOChequingPDFProcessor.CREDIT_SLICE].strip()
            next_debit = next_line[BMOChequingPDFProcessor.DEBIT_SLICE].strip()
            next_balance = next_line[BMOChequingPDFProcessor.BALANCE_SLICE].strip()

            next_tx_matcher = BMOChequingPDFProcessor.TX_PATTERN.fullmatch(next_desc)
            if next_tx_matcher is None and next_desc.strip() != '' and next_credit == '' and next_debit == '' and next_balance == '':
                note += ' ' + next_desc.strip()

        credit = sanitize_amount(credit)
        debit = sanitize_amount(debit)
        balance = sanitize_amount(balance)

        if credit:
            credit = int(credit)
        else:
            credit = None

        if debit:
            debit = int(debit)
        else:
            debit = None

        if balance:
            balance = int(balance)
        else:
            balance = None

        if 'Closing totals' in note:
            self.closing_debit = debit
            self.closing_credit = credit
            return None

        month = MONTHS.index(tx_matcher.group(1).lower()) + 1
        assert self.year, 'Statement year not set'
        tx_date = date(self.year, month, int(tx_matcher.group(2)))
        return Transaction(
            tx_date=tx_date,
            post_date=tx_date,
            payee=note,
            credit=credit,
            debit=debit,
            balance=balance,
            note=note,
        )

    def should_stop_processing_page(self, line: str) -> bool:
        return line.strip() == 'continued'

    def should_stop_processing_doc(self, line: str) -> bool:
        return 'Please report any errors' in line

    def post_process_transactions(self, transactions: list[Transaction]) -> list[Transaction]:
        total_credit = 0
        total_debit = 0
        for t in transactions:
            if t.credit is not None:
                self.logger.debug('%-55s  %8d  %8s  %8d', t.payee, t.credit, '', total_credit)
                total_credit += t.credit
            elif t.debit is not None:
                self.logger.debug('%-55s  %8s  %8d  %8d', t.payee, '', t.debit, total_debit)
                total_debit += t.debit
            else:
                assert False, f'Transaction has no credit or debit {t}'

        assert total_credit == self.closing_credit, f'Credit mismatch: {total_credit} != {self.closing_credit}'
        assert total_debit == self.closing_debit, f'Debit mismatch: {total_debit} != {self.closing_debit}'

        return transactions

class BMOMastercardPDFProcessor(PDFProcessor):
    DESC_SLICE = slice(None, 78)
    AMOUNT_SLICE = slice(78, 95)

    TX_PATTERN = re.compile(r'\s+([A-Z][a-z]{2})\.\s+(\d{1,2})\s+([A-Z][a-z]{2})\.\s+(\d{1,2})\s+(.+)')

    @classmethod
    def try_create_processor(cls, first_page: str, logger: logging.Logger) -> Self | None:
        if 'BMO' in first_page:
            logger.debug('Looks like a BMO statement')
            if 'Statement date' in first_page:
                logger.debug('Looks like a new version of a BMO Credit Card statement')
                return cls(logger)
        return None

    def __init__(self, logger: logging.Logger):
        super().__init__(logger)
        self.year = None
        self.opening_balance = None
        self.closing_balance = None

    def extract_text(self, page: pdfplumber.page.Page) -> str:
        return page.extract_text(layout=True, x_density=4.5, x_tolerance=1)

    def process_first_page(self, text: str) -> None:
        for line in text.split('\n'):
            if not self.opening_balance:
                m = re.fullmatch(r'\s+Previous\s+(?:total\s+)?balance,\s+[A-Z][a-z]{2}\.\s+\d{1,2},\s+\d{4}\s+\$([\d,]+.\d{2}\s+(CR)?).*', line)
                if m:
                    self.logger.info('Opening balance found: %s', m.group(1))
                    opening_balance = sanitize_amount(m.group(1))
                    if m.group(2):
                        self.opening_balance = -int(opening_balance.replace('CR', ''))
                    else:
                        self.opening_balance = int(opening_balance)

            if not self.year:
                m = re.fullmatch(r'.*\s+Statement\s+date\s+[A-Z][a-z]+\.\s+\d{1,2},\s+(\d{4}).*', line)
                if m:
                    self.logger.info('Year found: %s', m.group(1))
                    self.year = int(m.group(1))

            if not self.closing_balance:
                m = re.fullmatch(r'\s+Total\s+balance\s+\$([\d,]+.\d{2})\s+(CR)?.*', line)
                if m:
                    self.logger.info('Closing balance found: %s', m.group(1))
                    self.closing_balance = sanitize_amount(m.group(1))
                    if m.group(2):
                        self.closing_balance = -int(self.closing_balance.replace('CR', ''))
                    else:
                        self.closing_balance = int(self.closing_balance)

    def should_begin_processing_transaction(self, line: str) -> bool:
        return re.fullmatch(r'\s+DATE\s+DATE\s+DESCRIPTION\s+AMOUNT.*', line) is not None

    def prepare_new_transaction(self, line: str, prev_line: str | None, next_line: str | None) -> Transaction | None:
        desc = line[BMOMastercardPDFProcessor.DESC_SLICE]
        amount = line[BMOMastercardPDFProcessor.AMOUNT_SLICE]

        self.logger.debug('d: [%s]', desc)
        self.logger.debug('a: [%s]', amount)

        tx_matcher = BMOMastercardPDFProcessor.TX_PATTERN.fullmatch(desc)
        if tx_matcher is None:
            return None

        self.logger.debug('tx_matcher: %s', tx_matcher.groups())

        note = tx_matcher.group(5).strip()
        if next_line:
            next_desc = next_line[BMOMastercardPDFProcessor.DESC_SLICE]
            next_amount = next_line[BMOMastercardPDFProcessor.AMOUNT_SLICE].strip()

            next_tx_matcher = BMOMastercardPDFProcessor.TX_PATTERN.fullmatch(next_desc)
            if next_tx_matcher is None and next_desc.strip() != '' and next_amount == '':
                note += ' ' + next_desc.strip()

        amount = sanitize_amount(amount)
        if 'CR' in amount:
            credit = None
            debit = int(amount.replace('CR', ''))
        else:
            credit = int(amount)
            debit = None

        note = sanitize_string(note)

        tx_month = MONTHS.index(tx_matcher.group(1).lower()) + 1
        post_month = MONTHS.index(tx_matcher.group(3).lower()) + 1
        assert self.year, 'Statement year not set'
        tx_date = date(self.year, tx_month, int(tx_matcher.group(2)))
        post_date = date(self.year, post_month, int(tx_matcher.group(4)))
        return Transaction(
            tx_date=tx_date,
            post_date=post_date,
            payee=note,
            credit=credit,
            debit=debit,
            balance=None,
            note=note,
        )

    def should_stop_processing_page(self, line: str) -> bool:
        return '(continued on next page)' in line

    def should_stop_processing_doc(self, line: str) -> bool:
        if not self.closing_balance:
            closing_matcher = re.fullmatch(r'\s+Total\s+for\s+card\s+number\s+XXXX\s+XXXX\s+XXXX\s+\d{4}\s+\$([\d,]+\.\d{2})\s+', line)
            self.logger.debug(closing_matcher)
            if not closing_matcher:
                return False

            self.closing_balance = int(sanitize_amount(closing_matcher.group(1)))
            return True
        return False

    def post_process_transactions(self, transactions: list[Transaction]) -> list[Transaction]:
        assert self.opening_balance is not None, 'Opening balance not found'
        assert self.closing_balance is not None, 'Closing balance not found'
        total_amount = 0
        for t in transactions:
            if t.credit is not None:
                total_amount += t.credit
                self.logger.debug('%-60s  %8d  %8s  %8d', t.payee, t.credit, '', total_amount)
            elif t.debit is not None:
                total_amount -= t.debit
                self.logger.debug('%-60s  %8s  %8d  %8d', t.payee, '', t.debit, total_amount)
            else:
                assert False, f'Transaction has no credit or debit {t}'

        assert total_amount + self.opening_balance == self.closing_balance, 'Balance mismatch'
        return transactions

class OldBMOMastercardPDFProcessor(BMOMastercardPDFProcessor):
    DESC_SLICE = slice(None, 85)
    REF_SLICE = slice(88, 115)
    AMOUNT_SLICE = slice(118, 135)

    TX_PATTERN = re.compile(r'\s+([A-Z][a-z]{2})\.\s+(\d{1,2})\s+([A-Z][a-z]{2})\.\s+(\d{1,2})\s+(.+)')

    @classmethod
    def try_create_processor(cls, first_page: str, logger: logging.Logger) -> Self | None:
        if 'BMO' in first_page:
            logger.debug('Looks like a BMO statement')
            if 'Statement Date' in first_page:
                logger.debug('Looks like an old version of a BMO Credit Card statement')
                return cls(logger)
        return None

    def process_first_page(self, text: str) -> None:
        for line in text.split('\n'):
            if not self.year:
                m = re.fullmatch(r'.*\s+Statement\s+Date\s+[A-Z][a-z]+\.\s+\d{1,2},\s+(\d{4}).*', line)
                if m:
                    self.logger.info('Year found: %s', m.group(1))
                    self.year = int(m.group(1))

            if not self.opening_balance:
                m = re.fullmatch(r'.*\s+Previous\s+Balance,\s+[A-Z][a-z]{2}\.\s+\d{1,2},\s+\d{4}\s+\$([\d,]+.\d{2}).*', line)
                if m:
                    self.logger.info('Opening balance found: %s', m.group(1))
                    self.opening_balance = int(sanitize_amount(m.group(1)))

            if not self.closing_balance:
                m = re.fullmatch(r'.*\s+New\s+Balance,\s+[A-Z][a-z]{2}\.\s+\d{1,2},\s+\d{4}\s+\$([\d,]+.\d{2})(\s+CR)?.*', line)
                if m:
                    self.logger.info('Closing balance found: %s', m.groups())
                    closing_balance = sanitize_amount(m.group(1))
                    if m.group(2) and m.group(2).strip() == 'CR':
                        self.closing_balance = -int(closing_balance.replace('CR', ''))
                    else:
                        self.closing_balance = int(closing_balance)

        assert self.year, 'Statement year not set'
        assert self.opening_balance is not None, 'Opening balance not set'
        assert self.closing_balance is not None, 'Closing balance not set'

    def should_begin_processing_transaction(self, line: str) -> bool:
        return re.fullmatch(r'\s+DATE\s+DATE\s+DESCRIPTION\s+REFERENCE\sNO\.\s+AMOUNT.*', line) is not None

    def prepare_new_transaction(self, line: str, prev_line: str | None, next_line: str | None) -> Transaction | None:
        desc = line[OldBMOMastercardPDFProcessor.DESC_SLICE]
        reference = line[OldBMOMastercardPDFProcessor.REF_SLICE]
        amount = line[OldBMOMastercardPDFProcessor.AMOUNT_SLICE]

        self.logger.debug('d: [%s]', desc)
        self.logger.debug('r: [%s]', reference)
        self.logger.debug('a: [%s]', amount)

        tx_matcher = OldBMOMastercardPDFProcessor.TX_PATTERN.fullmatch(desc)
        if tx_matcher is None:
            return None

        self.logger.debug('tx_matcher: %s', tx_matcher.groups())

        note = tx_matcher.group(5).strip()

        amount = sanitize_amount(amount)
        if 'CR' in amount:
            credit = None
            debit = int(amount.replace('CR', ''))
        else:
            credit = int(amount)
            debit = None

        note = sanitize_string(note)

        tx_month = MONTHS.index(tx_matcher.group(1).lower()) + 1
        post_month = MONTHS.index(tx_matcher.group(3).lower()) + 1
        assert self.year, 'Statement year not set'
        tx_date = date(self.year, tx_month, int(tx_matcher.group(2)))
        post_date = date(self.year, post_month, int(tx_matcher.group(4)))
        return Transaction(
            tx_date=tx_date,
            post_date=post_date,
            payee=note,
            credit=credit,
            debit=debit,
            balance=None,
            note=note + ' ' + reference.strip(),
        )

class RBCChequingPDFProcessor(PDFProcessor):
    DESC_SLICE = slice(None, 60)
    CREDIT_SLICE = slice(68, 85)
    DEBIT_SLICE = slice(88, 110)

    TX_PATTERN = re.compile(r'\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(.+)')

    @classmethod
    def try_create_processor(cls, first_page: str, logger: logging.Logger) -> Self | None:
        if re.search(r'\s*Royal\s+Bank\s+of\s+Canada\s*', first_page):
            logger.info('Found RBC header')
            if re.search(r'.*Total\s+deposits.*', first_page):
                logger.info('Definitely an RBC bank account')
                return cls(logger)
        return None

    def __init__(self, logger: logging.Logger):
        super().__init__(logger)
        self.year = None
        self.opening_balance = None
        self.closing_balance = None
        self.last_transaction_date = None
        self.pending_tx = None

    def extract_text(self, page: pdfplumber.page.Page) -> str:
        return page.extract_text(layout=True, x_density=4.5, x_tolerance=1)

    def process_first_page(self, text: str) -> None:
        for line in text.split('\n'):
            if not self.opening_balance:
                m = re.fullmatch(r'\s+Your\s+opening\s+balance\s+(?:on\s+[A-Z][a-z]*\s+\d+,\s+\d{4}\s+)?((?:-\s*)?\$[\d,]+.\d{2}).*', line)
                if m:
                    self.logger.info('Opening balance found: %s', m.group(1))
                    opening_balance = sanitize_amount(m.group(1))
                    self.opening_balance = int(opening_balance)

            if not self.closing_balance:
                m = re.fullmatch(r'\s+Your\s+closing\s+balance\s+on\s+([A-Z][a-z]{2})[a-z]*\s+(\d+),\s+(\d{4})\s+=\s+((?:-\s*)?\$[\d,]+.\d{2}).*', line)
                if m:
                    self.logger.info('Closing balance found: %s', m.group(4))
                    closing_balance = sanitize_amount(m.group(4))
                    self.closing_balance = int(closing_balance)

                    self.logger.info('Year found: %s', m.group(3))
                    self.year = int(m.group(3))

        assert self.year, 'Statement year not set'
        assert self.opening_balance is not None, 'Opening balance not set'
        assert self.closing_balance is not None, 'Closing balance not set'

    def should_begin_processing_transaction(self, line: str) -> bool:
        return re.fullmatch(r'\s+Date\s+Description\s+Withdrawals\s+.*', line) is not None

    def prepare_new_transaction(self, line: str, prev_line: str | None, next_line: str | None) -> Transaction | None:
        if any([
            'No activity for this period' in line,
            'Opening Balance' in line
        ]):
            return None

        desc = line[RBCChequingPDFProcessor.DESC_SLICE]
        credit = line[RBCChequingPDFProcessor.CREDIT_SLICE]
        debit = line[RBCChequingPDFProcessor.DEBIT_SLICE]

        self.logger.debug('D: [%s]', desc)
        self.logger.debug('c: [%s]', credit)
        self.logger.debug('d: [%s]', debit)

        tx_matcher = RBCChequingPDFProcessor.TX_PATTERN.fullmatch(desc)
        if tx_matcher:
            tx_month = MONTHS.index(tx_matcher.group(2).lower()) + 1
            assert self.year, 'Statement year not set'
            note = tx_matcher.group(3).strip()
            tx_date = date(self.year, tx_month, int(tx_matcher.group(1)))
        else:
            note = desc.strip()
            assert self.last_transaction_date, 'Last transaction date not set'
            tx_date = self.last_transaction_date

        if credit.strip() == '' and debit.strip() == '':
            if self.pending_tx:
                self.pending_tx.payee += ' ' + note
                self.pending_tx.note += ' ' + note
            else:
                self.pending_tx = Transaction(tx_date, tx_date, note, 0, None, None, note)
            self.last_transaction_date = tx_date
            return None

        if credit.strip():
            credit = int(sanitize_amount(credit))
            debit = None
        elif debit.strip():
            credit = None
            debit = int(sanitize_amount(debit))
        else:
            raise ValueError('Invalid transaction')

        if self.pending_tx:
            self.pending_tx.payee += ' ' + note
            self.pending_tx.note += ' ' + note

            self.pending_tx.credit = credit
            self.pending_tx.debit = debit
            self.pending_tx.__post_init__()
            returning_tx = self.pending_tx
            self.pending_tx = None
        else:
            returning_tx = Transaction(tx_date, tx_date, note, credit, debit, None, note)

        self.last_transaction_date = tx_date

        returning_tx.payee = sanitize_string(returning_tx.payee)
        returning_tx.note = sanitize_string(returning_tx.note)

        return returning_tx

    def should_stop_processing_page(self, line: str) -> bool:
        return any([
            'Please check this Account Statement without delay' in line,
            'Closing Balance' in line,
            'No activity for this period' in line,
        ])

    def should_stop_processing_doc(self, line: str) -> bool:
        return False

    def post_process_transactions(self, transactions: list[Transaction]) -> list[Transaction]:
        assert self.opening_balance is not None, 'Opening balance not found'
        assert self.closing_balance is not None, 'Closing balance not found'
        total_amount = 0
        for t in transactions:
            if t.credit is not None:
                total_amount -= t.credit
                self.logger.debug('%-60s  %8d  %8s  %8d', t.payee, t.credit, '', total_amount)
            elif t.debit is not None:
                total_amount += t.debit
                self.logger.debug('%-60s  %8s  %8d  %8d', t.payee, '', t.debit, total_amount)
            else:
                assert False, f'Transaction has no credit or debit {t}'

        assert total_amount + self.opening_balance == self.closing_balance, \
                f'Balance mismatch ({total_amount} + {self.opening_balance}) ≠ {self.closing_balance}'
        return transactions

class RBCMastercardPDFProcessor(PDFProcessor):
    @classmethod
    def try_create_processor(cls, first_page: str, logger: logging.Logger) -> Self | None:
        if 'RBC' in first_page and 'Mastercard' in first_page:
            logger.info('Definitely an RBC Mastercard')
            return cls(logger)
        return None

    def __init__(self, logger: logging.Logger):
        super().__init__(logger)
        self.opening_balance = None
        self.closing_balance = None
        self.year = None

    def extract_text(self, page: pdfplumber.page.Page) -> str:
        cropped_box = (0, 0, 0.6 * page.width, page.height)
        cropped_page = page.within_bbox(cropped_box)

        return cropped_page.extract_text(
            layout=True,
            x_density=4,
            x_tolerance=1
        )

    def process_first_page(self, text: str) -> None:
        for line in text.split('\n'):
            if not self.year:
                m = re.match(r'.*STATEMENT\s+FROM\s+(\w+)\s+(\d+)(?:,\s+(\d{4}))?\s+TO\s+(\w+)\s+(\d+),\s+(\d{4}).*', line)
                if m:
                    self.logger.info('Year %s', m.group(6))
                    self.year = int(m.group(6))
            if not self.opening_balance:
                m = re.match(r'.*Previous\s+(?:Statement|Account)\s+Balance\s+((?:-)?\$[\d,]+\.\d{2}).*', line)
                if m:
                    self.logger.info('Opening balance %s', m.group(1))
                    self.opening_balance = int(sanitize_amount(m.group(1)))
            if not self.closing_balance:
                m = re.match(r'.*(?:NEW|CREDIT)\s+BALANCE\s+((?:-)?\$[\d,]+\.\d{2}).*', line)
                if m:
                    self.logger.info('Closing balance %s', m.group(1))
                    self.closing_balance = int(sanitize_amount(m.group(1)))

        assert self.year, 'Statement year not set'
        assert self.opening_balance is not None, 'Opening balance not set'
        assert self.closing_balance is not None, 'Closing balance not set'

    def should_begin_processing_transaction(self, line: str) -> bool:
        return re.match(r'.*DATE\s+DATE\s+.*', line) is not None

    def prepare_new_transaction(self, line: str, prev_line: str | None, next_line: str | None) -> Transaction | None:
        assert self.year, 'Statement year not set'

        desc = line[:74]
        amount = line[76:93]

        m = re.match(r'\s+([A-Z]{3})\s+(\d{2})\s+([A-Z]{3})\s+(\d{2})\s+(.+)', desc)
        if m:
            tx_month = MONTHS.index(m.group(1).lower()) + 1
            tx_date = date(self.year, tx_month, int(m.group(2)))
            post_month = MONTHS.index(m.group(3).lower()) + 1
            post_date = date(self.year, post_month, int(m.group(4)))
            payee = m.group(5).strip()

            if any([
                re.match(r'BALANCEPROTECTOR\s+PREMIUM', payee),
                re.match(r'AUTOMATIC\s+PAYMENT\s+-\s+THANK\s+YOU', payee),
            ]):
                note = None
            else:
                if not next_line or re.match(r'\d{11,23}', next_line[:76].strip()) is None:
                    raise ValueError('No transaction reference found')
                note = next_line[:76].strip()
        else:
            return None

        amount = int(sanitize_amount(amount))

        if amount > 0:
            credit = amount
            debit = None
        else:
            credit = None
            debit = -amount

        payee = sanitize_string(payee)

        return Transaction(
            tx_date=tx_date,
            post_date=post_date,
            payee=payee,
            credit=credit,
            debit=debit,
            balance=None,
            note=note,
        )

    def should_stop_processing_page(self, line: str) -> bool:
        return False

    def should_stop_processing_doc(self, line: str) -> bool:
        return re.match(r'.*NEW\s+BALANCE.*', line) is not None

    def post_process_transactions(self, transactions: list[Transaction]) -> list[Transaction]:
        assert self.opening_balance is not None, 'Opening balance not set'
        assert self.closing_balance is not None, 'Closing balance not set'

        new_balance = self.opening_balance
        for tx in transactions:
            if tx.credit is not None:
                new_balance += tx.credit
                self.logger.debug('%-60s  %8d  %8s  %8d', tx.payee, tx.credit, '', new_balance)
            elif tx.debit is not None:
                new_balance -= tx.debit
                self.logger.debug('%-60s  %8s  %8d  %8d', tx.payee, '', tx.debit, new_balance)

        assert new_balance == self.closing_balance, 'Balance mismatch'
        return transactions
