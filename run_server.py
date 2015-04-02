"""Instant Discourse.

Usage:
  run_server.py run <tornado_port> [--debug] [--autoreload]
  run_server.py init-redis
  run_server.py (-h | --help)

Options:
  --debug       Enable Tornado debugging.
  --autoreload  Restart on code changes.
  -h --help     Show this screen.
"""
import logging
import sys

from docopt import docopt
import redis
import tornado.httpserver
import tornado.ioloop

from instantdiscourse.server import get_app
from instantdiscourse.app import ChatNode

CAPACITY = 2**10
ERROR_RATE = 0.06
FILTER_SYNC_SECS = 5


def init_redis(redis, capacity, error_rate):
    redis.flushdb()
    ChatNode.zero_redis_filter(redis, capacity, error_rate, force=False)
    return


def init_logging():
    id_logger = logging.getLogger('instantdiscourse')
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(levelname)-8s (%(name)s:%(lineno)d): %(message)s')
    ch.setFormatter(formatter)
    id_logger.addHandler(ch)
    id_logger.setLevel(logging.DEBUG)

    tornado_logger = logging.getLogger('tornado')
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(name)s-%(levelname)s: %(message)s')
    ch.setFormatter(formatter)
    tornado_logger.addHandler(ch)
    tornado_logger.setLevel(logging.INFO)


def listen_forever(app, ws_port):
    http_server = tornado.httpserver.HTTPServer(app)
    http_server.listen(ws_port)
    app.start()


def main(args):
    redis_ = redis.StrictRedis(host='localhost', port=6379, db=0)

    if args['init-redis']:
        init_redis(redis_, CAPACITY, ERROR_RATE)
        return

    init_logging()

    app = get_app(
        args['<tornado_port>'],
        redis_, CAPACITY, ERROR_RATE, FILTER_SYNC_SECS,
        # app_kwargs:
        debug=args['--debug'],
        autoreload=args['--autoreload'],
    )
    listen_forever(app, args['<tornado_port>'])

if __name__ == "__main__":
    args = docopt(__doc__)
    main(args)
