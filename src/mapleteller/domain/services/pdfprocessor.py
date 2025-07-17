import logging
import re
from abc import ABC, abstractmethod
from datetime import date

import pdfplumber
import pdfplumber.page
from mapleteller.domain import Transaction


MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

class PDFProcessor(ABC):
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.year = None

    def process(self, file: str):
        self.logger.info('Processing file %s', file)
        transactions = []
        with pdfplumber.open(file) as pdf:
            for page in pdf.pages:
                self.logger.info('Processing page %d', page.page_number)
                transactions.extend(self._process_page(self.extract_text(page)))

        assert transactions and transactions[0].balance is not None
        transactions = transactions[1:]

        assert transactions and transactions[-1].balance is None

        total_credit = 0
        total_debit = 0
        for t in transactions[:-1]:
            if t.credit:
                total_credit += t.credit
            if t.debit:
                total_debit += t.debit

        assert total_credit == transactions[-1].credit
        assert total_debit == transactions[-1].debit

    def _process_page(self, text: str) -> list[Transaction]:
        lines = text.split('\n')
        transactions = []

        if self.year is None:
            for line in lines:
                self.year = self.extract_year(line)
                if self.year is not None:
                    self.logger.info('Statement year %d', self.year)
                    break

        assert self.year, 'Statement year not found'

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
            if self.should_stop_processing_page(lines[i]):
                break

            if i == 0:
                prev_line = None
            else:
                prev_line = lines[i - 1]

            if i + 1 < len(lines) and not self.should_stop_processing_page(lines[i + 1]):
                next_line = lines[i + 1]
            else:
                next_line = None

            self.logger.debug('[_] %s', lines[i])
            tx = self.prepare_new_transaction(lines[i], prev_line, next_line)
            if tx:
                self.logger.info('[T] %s', tx)
                transactions.append(tx)

        return transactions

    @abstractmethod
    def extract_text(self, page: pdfplumber.page.Page) -> str: ...

    @abstractmethod
    def extract_year(self, line: str) -> int | None: ...

    @abstractmethod
    def should_begin_processing_transaction(self, line: str) -> bool: ...

    @abstractmethod
    def prepare_new_transaction(self, line: str, prev_line: str | None, next_line: str | None) -> Transaction | None: ...

    @abstractmethod
    def should_stop_processing_page(self, line: str) -> bool: ...

class BMOChequingPDFProcessor(PDFProcessor):
    DESC_SLICE = slice(None, 68)
    CREDIT_SLICE = slice(70, 87)
    DEBIT_SLICE = slice(90, 107)
    BALANCE_SLICE = slice(109, 125)

    def extract_text(self, page: pdfplumber.page.Page) -> str:
        return page.extract_text(layout=True, x_density=4.5, x_tolerance=1)

    def extract_year(self, line: str) -> int | None:
        m = re.fullmatch(r'\s+For\s+the\s+period\s+ending\s+[A-Z][a-z]+\s+\d{2},\s+(\d{4}).*', line)
        if m:
            return int(m.group(1))
        return None

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

        tx_matcher = re.fullmatch(r'\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(.+)', desc)
        self.logger.debug('tx_matcher: %s', tx_matcher)
        if tx_matcher is None:
            return None

        note = tx_matcher.group(3).strip()
        if next_line:
            next_desc = next_line[BMOChequingPDFProcessor.DESC_SLICE]
            next_credit = next_line[BMOChequingPDFProcessor.CREDIT_SLICE].strip()
            next_debit = next_line[BMOChequingPDFProcessor.DEBIT_SLICE].strip()
            next_balance = next_line[BMOChequingPDFProcessor.BALANCE_SLICE].strip()

            next_tx_matcher = re.fullmatch(r'\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(.+)', next_desc)
            if next_tx_matcher is None and next_desc.strip() != '' and next_credit == '' and next_debit == '' and next_balance == '':
                note += ' ' + next_line[:65].strip()

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
        return 'Please report any errors' in line \
            or line.strip() == 'continued'

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
