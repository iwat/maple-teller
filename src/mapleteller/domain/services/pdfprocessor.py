import logging
import re
from abc import ABC, abstractmethod
from datetime import date

import pdfplumber
import pdfplumber.page
from mapleteller.domain import Transaction


MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

class PDFProcessor(ABC):
    @staticmethod
    def process(file: str, logger: logging.Logger) -> list[Transaction]:
        logger.info('Processing file %s', file)
        with pdfplumber.open(file) as pdf:
            with pdfplumber.open(file) as pdf:
                first_page = pdf.pages[0].extract_text(layout=True, x_tolerance=1)
                if 'BMO' in first_page:
                    logger.debug('Looks like a BMO Credit Card statement')
                    if 'Statement Date' in first_page:
                        logger.debug('Looks like an old version of a BMO Credit Card statement')
                        processor = OldBMOMastercardPDFProcessor(logger)
                    elif 'Statement date' in first_page:
                        logger.debug('Looks like a new version of a BMO Credit Card statement')
                        processor = BMOMastercardPDFProcessor(logger)
                    elif 'Summary of your account' in first_page:
                        logger.debug('Looks like a BMO Banking Account statement')
                        processor = BMOChequingPDFProcessor(logger)
                    else:
                        print(first_page)
                        raise ValueError('No idea what this is')
                elif 'Summary of your account' in first_page:
                    logger.debug('Looks like a BMO Banking Account statement')
                    processor = BMOChequingPDFProcessor(logger)
                else:
                    print(first_page)
                    raise ValueError('No idea what this is')

                transactions = []
                with pdfplumber.open(file) as pdf:
                    for page in pdf.pages:
                        text = processor.extract_text(page)
                        if page.page_number == 1:
                            processor.process_first_page(text)
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

        credit = credit.replace(',', '').replace('.', '').strip()
        debit = debit.replace(',', '').replace('.', '').strip()
        balance = balance.replace(',', '').replace('.', '').strip()

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
            transaction_date=tx_date,
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
                print(f'{t.payee:55s}  {t.credit:8d}  {" ":8s}  {total_credit:8d}')
                total_credit += t.credit
            elif t.debit is not None:
                print(f'{t.payee:55s}  {" ":8s}  {t.debit:8d}  {total_debit:8d}')
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
                    self.logger.info('Open balance found: %s', m.group(1))
                    opening_balance = m.group(1).replace(',', '').replace('.', '')
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
                    self.closing_balance = m.group(1).replace(',', '').replace('.', '')
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

        amount = amount.replace(',', '').replace('.', '').strip()
        if 'CR' in amount:
            credit = None
            debit = int(amount.replace('CR', ''))
        else:
            credit = int(amount)
            debit = None

        while '  ' in note:
            note = note.replace('  ', ' ')

        tx_month = MONTHS.index(tx_matcher.group(1).lower()) + 1
        post_month = MONTHS.index(tx_matcher.group(3).lower()) + 1
        assert self.year, 'Statement year not set'
        tx_date = date(self.year, tx_month, int(tx_matcher.group(2)))
        post_date = date(self.year, post_month, int(tx_matcher.group(4)))
        return Transaction(
            transaction_date=tx_date,
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

            self.closing_balance = int(closing_matcher.group(1).replace(',', '').replace('.', ''))
            return True
        return False

    def post_process_transactions(self, transactions: list[Transaction]) -> list[Transaction]:
        assert self.opening_balance is not None, 'Open balance not found'
        assert self.closing_balance is not None, 'Closing balance not found'
        total_amount = 0
        for t in transactions:
            if t.credit is not None:
                total_amount += t.credit
                print(f'{t.payee:55s}  {t.credit:8d}  {" ":8s}  {total_amount:8d}')
            elif t.debit is not None:
                total_amount -= t.debit
                print(f'{t.payee:55s}  {" ":8s}  {t.debit:8d}  {total_amount:8d}')
            else:
                assert False, f'Transaction has no credit or debit {t}'

        assert total_amount + self.opening_balance == self.closing_balance, 'Balance mismatch'
        return transactions

class OldBMOMastercardPDFProcessor(BMOMastercardPDFProcessor):
    DESC_SLICE = slice(None, 85)
    REF_SLICE = slice(88, 115)
    AMOUNT_SLICE = slice(118, 135)

    TX_PATTERN = re.compile(r'\s+([A-Z][a-z]{2})\.\s+(\d{1,2})\s+([A-Z][a-z]{2})\.\s+(\d{1,2})\s+(.+)')

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
                    self.logger.info('Open balance found: %s', m.group(1))
                    self.opening_balance = int(m.group(1).replace(',', '').replace('.', ''))

            if not self.closing_balance:
                m = re.fullmatch(r'.*\s+New\s+Balance,\s+[A-Z][a-z]{2}\.\s+\d{1,2},\s+\d{4}\s+\$([\d,]+.\d{2})\s+(CR)?.*', line)
                if m:
                    self.logger.info('Closing balance found: %s', m.groups())
                    closing_balance = m.group(1).replace(',', '').replace('.', '')
                    if m.group(2) == 'CR':
                        self.closing_balance = -int(closing_balance.replace('CR', ''))
                    else:
                        self.closing_balance = int(closing_balance)

            if self.year and self.opening_balance and self.closing_balance:
                break

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

        amount = amount.replace(',', '').replace('.', '').strip()
        if 'CR' in amount:
            credit = None
            debit = int(amount.replace('CR', ''))
        else:
            credit = int(amount)
            debit = None

        while '  ' in note:
            note = note.replace('  ', ' ')

        tx_month = MONTHS.index(tx_matcher.group(1).lower()) + 1
        post_month = MONTHS.index(tx_matcher.group(3).lower()) + 1
        assert self.year, 'Statement year not set'
        tx_date = date(self.year, tx_month, int(tx_matcher.group(2)))
        post_date = date(self.year, post_month, int(tx_matcher.group(4)))
        return Transaction(
            transaction_date=tx_date,
            post_date=post_date,
            payee=note,
            credit=credit,
            debit=debit,
            balance=None,
            note=note + ' ' + reference.strip(),
        )

class RBCChequingPDFProcessor(PDFProcessor):
    pass

#overrideDuplicates = True # True = assume all 'duplicate' transactions are valid
#debug = False # prints out one parsed PDF for you to manually test regex on
#
#regexes = {
#    'BMO': {
#        'txn': (r"^(?P<dates>(?:\w{3}(\.|)+ \d{1,2}\s*){2})"
#            r"(?P<description>.+)\s"
#            r"(?P<amount>-?[\d,]+\.\d{2})(?P<cr>(\-|\s*CR))?"),
#        'startyear': r'Statement period\s\w+\.?\s{1}\d+\,\s{1}(?P<year>[0-9]{4})',
#        'openbal': r'Previous balance.*(?P<balance>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?',
#        'closingbal': r'(?:Total) balance\s.*(?P<balance>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?'
#    },
#    'RBC': {
#        'txn': (r"^(?P<dates>(?:\w{3} \d{2} ){2})"
#            r"(?P<description>.+)\s"
#            r"(?P<amount>-?\$[\d,]+\.\d{2}-?)(?P<cr>(\-|\s?CR))?"),
#        'startyear': r'STATEMENT FROM .+(?P<year>-?\,.[0-9][0-9][0-9][0-9])',
#        'openbal': r'(PREVIOUS|Previous) (STATEMENT|ACCOUNT|Account) (BALANCE|Balance) (?P<balance>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?',
#        'closingbal': r'(?:NEW|CREDIT) BALANCE (?P<balance>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?'
#    },
#    'MFC': {
#        'txn': (r"^(?P<dates>(?:\d{2}\/\d{2} ){2})"
#            r"(?P<description>.+)\s"
#            r"(?P<amount>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?"),
#        'startyear': r'Statement Period: .+(?P<year>-?\,.[0-9][0-9][0-9][0-9])',
#        'openbal': r'(PREVIOUS|Previous) (BALANCE|Balance) (?P<balance>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?',
#        'closingbal': r'(?:New) Balance (?P<balance>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?'
#    },
#    'TD': {
#        'txn': (r"(?P<dates>(?:\w{3} \d{1,2} ){2})"
#            r"(?P<description>.+)\s"
#            r"(?P<amount>-?\$[\d,]+\.\d{2}-?)(?P<cr>(\-|\s?CR))?"),
#        'startyear': r'Statement Period: .+(?P<year>-?\,.[0-9][0-9][0-9][0-9])',
#        'openbal': r'(PREVIOUS|Previous) (STATEMENT|ACCOUNT|Account) (BALANCE|Balance) (?P<balance>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?',
#        'closingbal': r'(?:NEW|CREDIT) BALANCE (?P<balance>\-?\s?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?'
#    },
#    'AMEX': {
#        'txn': (r"(?P<dates>(?:\w{3} \d{1,2} ){2})"
#            r"(?P<description>.+)\s"
#            r"(?P<amount>-?[\d,]+\.\d{2}-?)(?P<cr>(\-|\s?CR))?"),
#        'startyear': r'(?P<year>-?\,.[0-9][0-9][0-9][0-9])',
#        'openbal': r'(PREVIOUS|Previous) (BALANCE|Balance) (?P<balance>-?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?',
#        'closingbal': r'(?:New|CREDIT) Balance (?P<balance>\-?\s?\$[\d,]+\.\d{2})(?P<cr>(\-|\s?CR))?'
#    },
#}
#
#def get_transactions(data_directory):
#    result = set()
#    for pdf_path in Path(data_directory).rglob('*.pdf'):
#        try:
#            if pdf_path.parts[-2] == TARGET_FI:
#                result |= _parse_visa(pdf_path)
#        except Exception as e:
#            print("Error for %s" % pdf_path)
#            print(e)
#    return result
#
#def _parse_visa(pdf_path):
#    result = set()
#    text = ""
#    with pdfplumber.open(pdf_path) as pdf:
#        print("------------------------------------------")
#        print(pdf_path)
#        for page in pdf.pages:
#            text += page.extract_text(x_tolerance=1)
#
#        if (debug):
#            print(text)
#            exit()
#
#        year = _get_start_year(text, TARGET_FI)
#        opening_bal = _get_opening_bal(text, TARGET_FI)
#        closing_bal = _get_closing_bal(text, TARGET_FI)
#        # add_seconds = 0
#
#        endOfYearWarning = False
#
#        # debugging transaction mapping - all 3 regex in 'txn' have to find a result in order for it to be considered a 'match'
#        for match in re.finditer(regexes[TARGET_FI]['txn'], text, re.MULTILINE):
#            match_dict = match.groupdict()
#            date = match_dict['dates'].replace('/', ' ') # change format to standard: 03/13 -> 03 13
#            date = date.split(' ')[0:2]  # Aug. 10 Aug. 13 -> ['Aug.', '10']
#            date[0] = date[0].strip('.') # Aug. -> Aug
#            date.append(str(year))
#            date = ' '.join(date) # ['Aug', '10', '2021'] -> Aug 10 2021
#
#            try:
#                date = datetime.strptime(date, '%b %d %Y') # try Aug 10 2021 first
#            except: # yes I know this is horrible, but this script runs once if you download your .csvs monthly, what do you want from me
#                date = datetime.strptime(date, '%m %d %Y') # if it fails, 08 10 2021
#
#            # need to account for current year (Jan) and previous year (Dec) in statements
#            endOfYearCheck = date.strftime("%m")
#
#            if (endOfYearCheck == '12' and endOfYearWarning == False):
#                endOfYearWarning = True
#            if (endOfYearCheck == '01' and endOfYearWarning):
#                date = date + relativedelta(years = 1)
#
#            if (match_dict['cr']):
#                print("Credit balance found in transaction: '%s'" % match_dict['amount'])
#                amount = -float("-" + match_dict['amount'].replace('$', '').replace(',', ''))
#            else:
#                amount = -float(match_dict['amount'].replace('$', '').replace(',', ''))
#
#            # checks description regex
#            if ('$' in match_dict['description'] and TARGET_FI != 'BMO'): # BMO doesn't have $'s in their descriptions, so this is safe
#                print("************" + match_dict['description'])
#                newAmount = re.search(r'(?P<amount>-?\$[\d,]+\.\d{2}-?)(?P<cr>(\-|\s?CR))?', match_dict['description'])
#                amount = -float(newAmount['amount'].replace('$', '').replace(',', ''))
#                match_dict['description'] = match_dict['description'].split('$', 1)[0]
#
#            transaction = Transaction(AccountType[TARGET_FI],
#                                      str(date.date().isoformat()),
#                                      match_dict['description'],
#                                      amount)
#            if (transaction in result):
#                if (overrideDuplicates):
#                    transaction.description = transaction.description + " 2"
#                    result.add(transaction)
#                else:
#                    prompt = input("Duplicate transaction found for %s, on %s for %f. Do you want to add this again? " % (transaction.description, transaction.date, transaction.amount)).lower()
#                    if (prompt == 'y'):
#                        transaction.description = transaction.description + " 2"
#                        result.add(transaction)
#                    else:
#                        print("Ignoring!")
#            else:
#                result.add(transaction)
#    _validate(closing_bal, opening_bal, result)
#    return result
#
#def _validate(closing_bal, opening_bal, transactions):
#    # spend transactions are negative numbers.
#    # net will most likely be a neg number unless your payments + cash back are bigger than spend
#    # outflow is less than zero, so purchases
#    # inflow is greater than zero, so payments/cashback
#
#    # closing balance is a positive number
#    # opening balance is only negative if you have a CR, otherwise also positive
#    net = round(sum([r.amount for r in transactions]), 2)
#    outflow = round(sum([r.amount for r in transactions if r.amount < 0]), 2)
#    inflow = round(sum([r.amount for r in transactions if r.amount > 0]), 2)
#    if round(opening_bal - closing_bal, 2) != net:
#        print("* the diff is: %f vs. %f" % (opening_bal - closing_bal, net))
#        print(f"* Opening reported at {opening_bal}")
#        print(f"* Closing reported at {closing_bal}")
#        print(f"* Transactions (net/inflow/outflow): {net} / {inflow} / {outflow}")
#        print("* Parsed transactions:")
#        for t in sorted(list(transactions), key=lambda t: t.date):
#            print(t)
#        raise AssertionError("Discrepancy found, bad parse :(. Not all transcations are accounted for, validate your transaction regex.")
#
#def _get_start_year(pdf_text, fi):
#    print("Getting year...")
#    match = re.search(regexes[fi]['startyear'], pdf_text, re.IGNORECASE)
#    year = int(match.groupdict()['year'].replace(', ', ''))
#    print("YEAR IS: %d" % year)
#    return year
#
#
#def _get_opening_bal(pdf_text, fi):
#    print("Getting opening balance...")
#    match = re.search(regexes[fi]['openbal'], pdf_text)
#    if (match.groupdict()['cr'] and '-' not in match.groupdict()['balance']):
#        balance = float("-" + match.groupdict()['balance'].replace('$', ''))
#        print("Patched credit balance found for opening balance: %f" % balance)
#        return balance
#
#    balance = float(match.groupdict()['balance'].replace(',', '').replace('$', ''))
#    print("Opening balance: %f" % balance)
#    return balance
#
#
#def _get_closing_bal(pdf_text, fi):
#    print("Getting closing balance...")
#    match = re.search(regexes[fi]['closingbal'], pdf_text)
#    if (match.groupdict()['cr'] and '-' not in match.groupdict()['balance']):
#        balance = float("-" + match.groupdict()['balance'].replace('$', ''))
#        print("Patched credit balance found for closing balance: %f" % balance)
#        return balance
#
#    balance = float(match.groupdict()['balance'].replace(',', '').replace('$', '').replace(' ', ''))
#    print("Closing balance: %f" % balance)
#    return balance
#
