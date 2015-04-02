import logging
import sys

from nose.tools import nottest


@nottest
def configure_test_logging():
    logger = logging.getLogger()
    ch = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(name)s:%(levelname)s: %(message)s')
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.setLevel(logging.DEBUG)


def setup_package():
    configure_test_logging()
