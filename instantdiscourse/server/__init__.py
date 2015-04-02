from collections import Counter, namedtuple
from contextlib import contextmanager
import json
import logging
import pprint
from types import MethodType

from tornado import gen, ioloop, web, websocket, template
from tornado_cors import CorsMixin
import toro

from ..app import ChatNode

max_waiting_partners = 100
custom_message_type = 'INSTANT_DISCOURSE'


class HashEntry(namedtuple('HashEntry', 'reporter_cid speaker_cid hash')):
    __slots__ = ()

    def get_symmetric(self, partner_cid):
        """Return a HashEntry that must be seen to verify that the instance has been said.

        This means swapping reporter_cid for partner_cid."""
        return self._replace(reporter_cid=partner_cid)


@contextmanager
def log_exceptions():
    try:
        yield
    except:
        logging.getLogger(__name__).exception("log_exception")
        raise


class TestWebSocketHandlerMixin(object):
    """Base class for testing handlers that exposes the on_close event.

    This allows for deterministic cleanup of the associated socket.
    """
    # source:
    # https://github.com/tornadoweb/tornado/blob/26cb9b3fa67ef3282414a86743ee2e16c81913c3/tornado/test/websocket_test.py#L33
    # it'd be cool if we could mix this in dynamically only in tests

    def initialize(self, *args, **kwargs):
        super(TestWebSocketHandlerMixin, self).initialize(*args, **kwargs)

        self.__close_future = None
        future_gen = kwargs.pop('close_future_gen', None)
        if future_gen is not None:
            self.__close_future = future_gen.next()

    def on_close(self, *args, **kwargs):
        if self.__close_future is not None:
            self.__close_future.set_result((self.close_code, self.close_reason))

        return super(TestWebSocketHandlerMixin, self).on_close(*args, **kwargs)


class PeerJSHandler(websocket.WebSocketHandler):
    """Handles basic Peerjs signaling features."""

    cid_handlers = {}
    # should define peerjs message types here

    def initialize(self, node, **kwargs):
        self.node = node

        # this doesn't actually do what you want -- it'll print the bound method's MRO class
        self.log = logging.getLogger("%s:%s" % (__name__, self.__class__.__name__))

    def check_origin(self, origin):
        return True
        #  parsed_origin = urllib.parse.urlparse(origin)
        #  return parsed_origin.netloc.endswith(".mydomain.com")

    def open(self):
        """
        qstring: id
        qstring: token
        qstring: key
        """
        self.log.info("open: %r", self.request.query_arguments)

        if 'id' not in self.request.query_arguments:
            self.write_message(json.dumps({
                'type': 'ERROR',
                'payload': {
                    'msg': 'Expected an id in the querystring.'
                }
            }))
            self.close()
            return

        cid = self.get_query_argument('id')

        assert cid in self.node.cids_allocated
        self.node.set_cid_in_use(cid)

        self.cid = cid
        self.cid_handlers[cid] = self

        self.write_message(json.dumps({
            'type': 'OPEN'
        }))

    def on_message(self, message):
        """
        Peerjs messages always have keys type, payload, dst.

        Return True if we handled a message, False otherwise.
        """
        message = json.loads(message)
        self.log.info("on_message for %s, type %s", self.cid, message['type'])
        self.log.debug("message: %s", pprint.pformat(message))
        if message['type'] == custom_message_type:
            return False

        to_cid = message['dst']

        forward_message = {
            'type': message['type'],
            'src': self.cid,
            'dst': to_cid,
            'payload': message['payload'],
        }

        # I'm pretty sure you can get out of order messages somehow?
        assert to_cid in self.node.cids_in_use
        assert to_cid in self.cid_handlers

        self.cid_handlers[to_cid].write_message(json.dumps(forward_message))
        return True

    def on_close(self):
        self.log.info("on_close for %s", self.cid)
        del self.cid_handlers[self.cid]
        self.node.remove_cid_in_use(self.cid)


class ChatHandler(TestWebSocketHandlerMixin, PeerJSHandler):
    """
    This handler injects our custom signaling into the PeerJS stream.
    """

    partner_queue = toro.Queue(max_waiting_partners)

    def initialize(self, *args, **kwargs):
        super(ChatHandler, self).initialize(*args, **kwargs)
        self.partner_cid = None
        self.hash_counter = None

    def on_close(self):
        super(ChatHandler, self).on_close()

        # if our client was on the queue, remove them.
        try:
            self.partner_queue.queue.remove(self.cid)
            self.log.info("removed own client from queue")
        except ValueError:
            pass  # they weren't on the queue

    @gen.coroutine
    def on_message(self, message):
        with log_exceptions():
            if super(ChatHandler, self).on_message(message):
                # a PeerJS message was handled
                return

            message = json.loads(message)

            if message['subtype'] == 'get-partner':
                assert self.cid
                # self.partner_cid doesn't have to be None here
                # (at least in the current protocol) because there's no
                # explicit way to report a disconnect.
                self.log.info("about to call _wait_for_partner %s", self.cid)

                # to do a timeout, you could just use gen.sleep(_wait_for_partner())?
                yield self._wait_for_partner()

            elif message['subtype'] == 'me-hash':
                entry = HashEntry(self.cid, self.cid, message['hash'])
                self._handle_new_entry(entry)

            elif message['subtype'] == 'them-hash':
                entry = HashEntry(self.cid, self.partner_cid, message['hash'])
                self._handle_new_entry(entry)

            elif message['subtype'] == 'get-num-clients':
                self._send(self.cid, {'subtype': 'num-clients'})

            else:
                self.log.warning("unrecognized id message subtype")

    def _match_hash(self, hash_entry):
        """Return True if the entry has been reported by both clients, False otherwise."""

        self.log.info("_match_hash(%s) with %s",
                      hash_entry, self.hash_counter)

        symmetric = hash_entry.get_symmetric(self.partner_cid)

        if symmetric in self.hash_counter:
            self.hash_counter[symmetric] -= 1

            assert self.hash_counter[symmetric] >= 0

            if self.hash_counter[symmetric] == 0:
                del self.hash_counter[symmetric]

            return True

        self.hash_counter[hash_entry] += 1
        return False

    def _handle_new_entry(self, hash_entry):
        """Return True if this entry was matched.
        In this case, the entry will also be checked for a penalty and recorded.

        Return False otherwise.
        """
        if self._match_hash(hash_entry):
            if self.node.has_been_said(hash_entry.hash):
                self._send_penalty(hash_entry)
            else:
                # If this hash has been said, there's no reason to record it again.
                self.node.record_as_said(hash_entry.hash)
            return True

        return False

    def _send_penalty(self, hash_entry):
        # If the hash entry is built from a me-hash, the reporter and speaker
        # will be the same.
        receiver_cid = hash_entry.speaker_cid
        if hash_entry.speaker_cid == self.cid:
            partner_cid = self.partner_cid
        else:
            partner_cid = self.cid

        self.log.info("penalty for %r after saying %r",
                      receiver_cid, hash_entry.hash)

        self._send(receiver_cid, {
            'subtype': 'you-penalty',
            'duration_secs': 2,
        })

        self._send(partner_cid, {
            'subtype': 'them-penalty',
            'duration_secs': 2,
        })

    def _send(self, cid, message, attach_clients=True):
        # Always mutates message['type'].
        # Mutates message['num_clients'] if attach_clients.

        message['type'] = custom_message_type

        if attach_clients:
            message['num_clients'] = len(self.cid_handlers)

        self.cid_handlers[cid].write_message(json.dumps(message))

    @gen.coroutine
    def _wait_for_partner(self):
        with log_exceptions():
            self.log.info("_wait_for_partner: %s", self.cid)
            yield self.partner_queue.put(self.cid)

            # If there is a match on the queue, immediately notify
            # the waiting clients.
            # Tornado is single-threaded, so Toro queues have no
            # race on queue size.
            # This will probably match the same people over and over, though.
            if self.partner_queue.qsize() >= 2:
                # peerjs doesn't need both peers to call connect -- that'll
                # end with two separate connections.
                # So, we arbitrarily pick one peer to connect to the other.
                leader = self.partner_queue.get_nowait()
                follower = self.partner_queue.get_nowait()

                self.log.info("matched: (%s, %s)", leader, follower)

                self._send(leader, {
                    'subtype': 'match',
                    'match_cid': follower,
                })

                self.cid_handlers[leader].partner_cid = follower
                self.cid_handlers[follower].partner_cid = leader

                #TODO are they both supposed to have the same instance!?
                hash_counter = Counter()
                self.cid_handlers[leader].hash_counter = hash_counter
                self.cid_handlers[follower].hash_counter = hash_counter
            else:
                self.log.info("unable to match right now")


class GetCidHandler(CorsMixin, web.RequestHandler):
    CORS_HEADERS = ', '.join(['Accept', 'Accept-Version', 'Content-Type', 'Api-Version'])
    CORS_ORIGIN = '*'
    CORS_CREDENTIALS = True  # needed?

    def initialize(self, node):
        self.node = node

    def get(self):
        self.write(self.node.get_unused_cid())


class TestIndexHandler(web.RequestHandler):
    # Used during selenium tests to template the test server into the index.
    loader = None

    def initialize(self, template_path, **index_template_kwargs):
        if self.__class__.loader is None:
            self.__class__.loader = template.Loader(template_path)

        self.index_template_kwargs = index_template_kwargs

    def get(self):
        index = self.loader.load('index.html').generate(
            **self.index_template_kwargs)
        self.write(index)


class FaviconHandler(web.RequestHandler):
    def get(self):
        self.write('')


def get_app(tornado_port,
            redis, capacity, error_rate, filter_sync_secs,
            serve_index=False, template_path=None, index_template_kwargs=None,
            close_future_gen=None, **app_kwargs):
    """
    Return an Application with _id_node set to a new Node.

    filter_sync_secs: in seconds, how often to sync this node's filter from redis.
    close_future_gen: a generator that yields Futures forever. When closing a websocket,
      the server will set a Future's result. See TestWebSocketHandlerMixin.
    **app_kwargs: passed through to Application.
    """
    logging.getLogger("%s.get_app" % __name__).info("building app:\n%s", pprint.pformat(locals()))

    node = ChatNode(redis,
                    capacity,
                    error_rate)

    handlers = [
        (r'/favicon.ico', FaviconHandler),
        (r'/peerjs', ChatHandler, {
            'close_future_gen': close_future_gen,
            'node': node,
        }),
        (r'/peerjs/id', GetCidHandler, {'node': node}),
    ]

    if serve_index:
        index_handler_kwargs = {}
        index_handler_kwargs['template_path'] = template_path
        index_handler_kwargs.update(index_template_kwargs)
        handlers.append((r'/', TestIndexHandler, index_handler_kwargs))

    app = web.Application(handlers, **app_kwargs)

    sync_callback = ioloop.PeriodicCallback(
        node.sync_filter_from_redis, filter_sync_secs * 1000,
        io_loop=ioloop.IOLoop.instance()
    )

    for fieldname in ['_id_node', '_sync_periodic_calback', 'start']:
        # Make sure we're not accidentally overriding anything.
        assert not hasattr(app, fieldname), "%r has field %s" % (app, fieldname)

    app._id_node = node
    app._sync_periodic_callback = sync_callback

    def start(self):
        self._sync_periodic_callback.start()
        ioloop.IOLoop.instance().start()

    app.start = MethodType(start, app)

    return app
