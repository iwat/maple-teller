import argparse
import logging
import sys

from mapleteller.domain.services import PDFProcessor


def main():
    logging.basicConfig(level=logging.INFO)
    module_logger = logging.getLogger('mapleteller')

    parser = argparse.ArgumentParser(description='Process bank statements')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('file', nargs='?', help='Path to the statement file')

    args = parser.parse_args()

    if args.debug:
        module_logger.setLevel(logging.DEBUG)
    else:
        module_logger.setLevel(logging.INFO)
    PDFProcessor.process(args.file, module_logger)
