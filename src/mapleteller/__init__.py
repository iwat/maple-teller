import argparse
import logging
import sys

from mapleteller.domain.services import BMOChequingPDFProcessor


def main():
    logging.basicConfig(level=logging.INFO)
    module_logger = logging.getLogger('mapleteller')

    parser = argparse.ArgumentParser(description='Process bank statements')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('type', nargs='?', help='Statement type (bmo_chequing, rbc_chequing, bmo_mastercard, rbc_mastercard, rbc_invest)')
    parser.add_argument('file', nargs='?', help='Path to the statement file')

    args = parser.parse_args()

    if args.debug:
        module_logger.setLevel(logging.DEBUG)
    else:
        module_logger.setLevel(logging.INFO)

    match args.type:
        case 'bmo_chequing':
            BMOChequingPDFProcessor(module_logger).process(args.file)
        case 'rbc_chequing':
            pass
            #RBCChequingPDFProcessor(module_logger).process(sys.argv[2])
        case 'bmo_mastercard':
            pass
            #BMOMastercardPDFProcessor(module_logger).process(sys.argv[2])
        case 'rbc_mastercard':
            pass
            #RBCCardPDFProcessor(module_logger).process(sys.argv[2])
        case 'rbc_invest':
            pass
            #RBCInvestPDFProcessor(module_logger).process(sys.argv[2])
        case '_':
            print(f'Error: "{type}" is not a valid type')
            print('Valid types: bmo_chequing, rbc_chequing, bmo_mastercard, rbc_mastercard, rbc_invest')
            sys.exit(1)
