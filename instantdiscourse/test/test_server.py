from collections import namedtuple
import json
import threading

from concurrent import futures
import fakeredis
from nose.plugins.attrib import attr
import mock
import psutil
import redis
from tornado import gen
from tornado.httpclient import AsyncHTTPClient
from tornado.concurrent import Future
from tornado.testing import AsyncHTTPTestCase, gen_test
import tornado.websocket
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from instantdiscourse.app import ChatNode
from instantdiscourse.server import get_app as get_id_app
from instantdiscourse.server import (
    ChatHandler, custom_message_type, HashEntry
)


class SequentialClientIDMixin(object):
    """Make ChatNode return sequential ids.

    Must be mixed into a TestCase.
    """
    client_id = 0

    def setUp(self):
        cls = SequentialClientIDMixin  # for brevity
        super(cls, self).setUp()

        cls.client_id = 0
        cls.patch = mock.patch.object(ChatNode, '_get_cid', side_effect=self._next_client_id)
        cls.patch.__enter__()
        self.addCleanup(cls.patch.__exit__)

    def _next_client_id(self, *args, **kwargs):
        cls = SequentialClientIDMixin
        cid = cls.client_id
        cls.client_id += 1
        return str(cid)


class TornadoWsClientTestCase(AsyncHTTPTestCase):
    # adapted from:
    # https://github.com/tornadoweb/tornado/blob/26cb9b3fa67ef3282414a86743ee2e16c81913c3/tornado/test/websocket_test.py#L90

    def setUp(self):
        self.future_gen = FutureGen()
        self.futures = {}  # ws -> Future

        # Tornado calls get_app in their setUp,
        # and our get_app needs self.future_gen.
        super(TornadoWsClientTestCase, self).setUp()

    @gen.coroutine
    def connect_client(self, protocol, path):
        client = yield self._client_connect_impl(
            '%s://localhost:%d%s' % (protocol, self.get_http_port(), path),
        )
        self.futures[client] = self.future_gen.generated[-1]
        raise gen.Return(client)

    @gen.coroutine
    def _client_connect_impl(self, url):
        client = yield tornado.websocket.websocket_connect(url)
        raise gen.Return(client)

    @gen.coroutine
    def close(self, client):
        """Close a client and wait for the server side.

        If we don't wait here, there are sometimes leak warnings in the
        tests.

        If client.close is safe to call more than once, this is as well.
        """
        client.close()

        future = self.futures[client]
        if not future.done():
            yield future

    @gen.coroutine
    def close_all_clients(self):
        for client in self.futures:
            yield self.close(client)

    def wait_until(self, condition, interval=.1, future=None, **kwargs):
        """Return a Future that completes when condition() returns a truthy value."""
        # Could potentially backoff.

        if future is None:
            future = Future()

        result = condition(**kwargs)

        if result:
            future.set_result(result)
        else:
            self.io_loop.call_later(
                interval, self.wait_until, condition,
                interval, future, **kwargs)

        return future


class FutureGen(object):
    def __init__(self):
        self.generated = []

    def __iter__(self):
        return self

    def next(self):
        while True:
            f = Future()
            self.generated.append(f)
            return f

Client = namedtuple('Client', 'cid ws')


class ServerTornadoWsClientTestCase(SequentialClientIDMixin, TornadoWsClientTestCase):
    capacity = 4
    error_rate = .08

    def _get_redis(self):
        return fakeredis.FakeStrictRedis()

    def get_app(self):
        redis = self._get_redis()
        ChatNode.zero_redis_filter(redis, self.capacity, self.error_rate, force=True)
        return get_id_app(
            self.get_http_port(),
            redis, self.capacity, self.error_rate, 1,
            True, '../indextemplate',
            {'tornado_port': self.get_http_port(), 'tornado_host': 'localhost'},
            self.future_gen,
        )

    def setUp(self):
        super(ServerTornadoWsClientTestCase, self).setUp()
        self._app._id_node.redis.flushdb()
        self._app._id_node._overwrite_redis_filter()

    def tearDown(self):
        # We need access to the ioloop while closing; do it before Tornado's tearDown.
        self.close_all_clients(callback=self.stop)
        self.wait()
        super(ServerTornadoWsClientTestCase, self).tearDown()

    @gen.coroutine
    def _connect_new_client(self):
        client = AsyncHTTPClient(self.io_loop)
        res = yield client.fetch(self.get_url('/peerjs/id'))
        cid = res.body

        ws = yield self.connect_client('ws', '/peerjs?id=%s' % cid)

        res = yield ws.read_message()
        self.assertEqual(res, json.dumps({'type': 'OPEN'}))
        raise gen.Return(Client(cid, ws))


@attr('unit')
class ServerTornadoClientTests(ServerTornadoWsClientTestCase):
    @gen_test
    def test_connect(self):
        client = yield self._connect_new_client()

        # close early to test close_all_clients' multiple-call safety.
        yield self.close(client.ws)

    @gen_test
    def test_get_partner(self):
        clients = []
        for _ in xrange(2):
            c = yield self._connect_new_client()
            c.ws.write_message(json.dumps({
                'type': custom_message_type,
                'subtype': 'get-partner',
            }))
            clients.append(c)

        res = yield clients[0].ws.read_message()
        self.assertEqual(json.loads(res)['match_cid'], clients[1].cid)

    @gen_test
    def test_successful_hash_match(self):
        clients = []
        for _ in xrange(2):
            c = yield self._connect_new_client()
            c.ws.write_message(json.dumps({
                'type': custom_message_type,
                'subtype': 'get-partner',
            }))
            clients.append(c)

        res = yield clients[0].ws.read_message()
        self.assertEqual(json.loads(res)['match_cid'], clients[1].cid)

        hash_counter = ChatHandler.cid_handlers[clients[0].cid].hash_counter

        self.assertIs(
            hash_counter,
            ChatHandler.cid_handlers[clients[1].cid].hash_counter
        )

        clients[0].ws.write_message(json.dumps({
            'type': custom_message_type,
            'subtype': 'me-hash',
            'hash': 'the hash',
        }))

        hash = HashEntry(clients[0].cid, clients[0].cid, 'the hash')
        # Wait for the hash to be matched.
        yield self.wait_until(lambda: hash_counter[hash] == 1)
        self.assertEqual(hash_counter[hash], 1)

        clients[1].ws.write_message(json.dumps({
            'type': custom_message_type,
            'subtype': 'them-hash',
            'hash': 'the hash',
        }))

        # Wait for the hash to be recorded.
        yield self.wait_until(lambda: hash not in hash_counter)
        self.assertEqual(hash_counter[hash], 0)

    @gen_test
    def test_penalty_me_first(self):
        clients = []
        for _ in xrange(2):
            c = yield self._connect_new_client()
            c.ws.write_message(json.dumps({
                'type': custom_message_type,
                'subtype': 'get-partner',
            }))
            clients.append(c)

        res = yield clients[0].ws.read_message()
        self.assertEqual(json.loads(res)['match_cid'], clients[1].cid)

        for _ in xrange(2):
            clients[0].ws.write_message(json.dumps({
                'type': custom_message_type,
                'subtype': 'me-hash',
                'hash': 'penalty hash',
            }))

            clients[1].ws.write_message(json.dumps({
                'type': custom_message_type,
                'subtype': 'them-hash',
                'hash': 'penalty hash',
            }))

        my_penalty = yield clients[0].ws.read_message()
        their_penalty = yield clients[1].ws.read_message()

        self.assertEqual(
            json.loads(my_penalty),
            {'type': custom_message_type,
             'subtype': 'you-penalty',
             'num_clients': 2,
             'duration_secs': 2})

        self.assertEqual(
            json.loads(their_penalty),
            {'type': custom_message_type,
             'subtype': 'them-penalty',
             'num_clients': 2,
             'duration_secs': 2})

    @gen_test
    def test_penalty_them_first(self):
        clients = []
        for _ in xrange(2):
            c = yield self._connect_new_client()
            c.ws.write_message(json.dumps({
                'type': custom_message_type,
                'subtype': 'get-partner',
            }))
            clients.append(c)

        res = yield clients[0].ws.read_message()
        self.assertEqual(json.loads(res)['match_cid'], clients[1].cid)

        for _ in xrange(2):
            clients[1].ws.write_message(json.dumps({
                'type': custom_message_type,
                'subtype': 'them-hash',
                'hash': 'penalty hash',
            }))

            # Something weird with ordering happens here.
            # If we don't spin the ioloop, client 0's me-hash
            # will be processed first.
            yield gen.moment

            clients[0].ws.write_message(json.dumps({
                'type': custom_message_type,
                'subtype': 'me-hash',
                'hash': 'penalty hash',
            }))

        my_penalty = yield clients[0].ws.read_message()
        their_penalty = yield clients[1].ws.read_message()

        self.assertEqual(
            json.loads(my_penalty),
            {'type': custom_message_type,
             'subtype': 'you-penalty',
             'num_clients': 2,
             'duration_secs': 2})

        self.assertEqual(
            json.loads(their_penalty),
            {'type': custom_message_type,
             'subtype': 'them-penalty',
             'num_clients': 2,
             'duration_secs': 2})


class ServerSeleniumTestCase(ServerTornadoWsClientTestCase):
    def _get_redis(self):
        # db 0 used by test server, currently
        return redis.StrictRedis(host='localhost', port=6379, db=1)

    def _get_driver(self):
        """Return a new WebDriver.

        Cleanup is handled automatically.
        """
        # Enables getting chrome log:
        #   driver.get_log('browser') -> ['log line']
        d = DesiredCapabilities.CHROME
        d['loggingPrefs'] = {'browser': 'ALL'}
        driver = webdriver.Chrome('/opt/selenium/chromedriver-2.13',
                                  desired_capabilities=d)

        self.addCleanup(driver.quit)
        return driver

    @gen.coroutine
    def _client_connect_impl(self, url):
        driver = self._get_driver()
        yield self.executor.submit(driver.get, url)
        raise gen.Return(driver)

    def setUp(self):
        super(ServerSeleniumTestCase, self).setUp()
        self.assertEqual(threading.active_count(), 1)

        self.assertEqual(len(ChatHandler.cid_handlers), 0)
        self.executor = futures.ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.executor.shutdown, wait=True)

    def tearDown(self):
        super(ServerSeleniumTestCase, self).tearDown()

        # All clients should have been closed, which should remove the handlers.
        self.assertEqual(len(ChatHandler.cid_handlers), 0)

        # By default, cleanups are run after tearDown finishes.
        # driver.quit should run before our check for chrome leaking, though,
        # so we run cleanups now instead.
        # There's no harm in calling it more than once.
        self.doCleanups()

        for proc in psutil.process_iter():
            try:
                if 'chrome' in proc.name():
                    msg = "chrome resource leak detected! killing %s" % proc
                    print msg
                    proc.kill()
                    raise AssertionError(msg)
            except psutil.NoSuchProcess:
                pass


@attr('end-to-end')
class ServerSeleniumTests(ServerSeleniumTestCase):
    @gen_test
    def test_get_clients_initially(self):
        num_clients = 2
        for i in xrange(num_clients):
            driver = yield self.connect_client('http', '/')

            def clients_filled(driver=driver):
                # We only continue on truthy values, so we'll wait until
                # we don't see the empty string.
                return driver.find_element_by_id('num-clients').text

            client_text = yield self.wait_until(clients_filled)
            self.assertEqual(int(client_text), i + 1)

    @gen_test
    def test_single_client_gets_cid(self):
        driver = yield self.connect_client('http', '/')
        client_id = driver.find_element_by_id('pid').text

        self.assertEqual(len(ChatHandler.cid_handlers), 1)
        self.assertEqual(ChatHandler.cid_handlers[client_id].cid, client_id)

    @gen_test
    def test_n_clients_get_cids(self):
        num_clients = 2
        client_ids = {}
        for _ in xrange(num_clients):
            driver = yield self.connect_client('http', '/')
            client_ids[driver] = driver.find_element_by_id('pid').text

        self.assertEqual(len(ChatHandler.cid_handlers), num_clients)
        for client_id in client_ids.values():
            self.assertEqual(ChatHandler.cid_handlers[client_id].cid, client_id)

    @staticmethod
    def _peer_connection_established(recv_driver, requester_cid):
        try:
            return recv_driver.find_element_by_id('connect-' + requester_cid)
        except NoSuchElementException:
            return None

    # why do these take so long? The timeouts happen on _wait_for_peer

    @gen_test(timeout=10)
    def test_two_client_chat(self):
        clients = []  # (driver, cid)

        # Connect + request-partner from one client, then the other.
        for _ in xrange(2):
            driver = yield self.connect_client('http', '/')
            driver.find_element_by_id('get-partner').click()
            clients.append((driver, driver.find_element_by_id('pid').text))

        yield self.wait_until(
            self._peer_connection_established,
            recv_driver=clients[0][0], requester_cid=clients[1][1])

        # Send a message from the first to the second.
        chatbox = clients[0][0].find_element_by_id('text')
        chatbox.send_keys("message from client 0", Keys.RETURN)
        recv_message_from = WebDriverWait(clients[1][0], 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "from-peer"))
        )
        self.assertEqual(recv_message_from.text, 'Peer: message from client 0')
        self.assertTrue('You: message from client 0' in clients[0][0].page_source)

    @gen_test(timeout=15)
    def test_multi_client_chat(self):
        clients = []  # (driver, cid)

        # Connect + request-partner from all three.
        # The first two will get connected to eachother and the third will queue.
        for _ in xrange(3):
            driver = yield self.connect_client('http', '/')
            driver.find_element_by_id('get-partner').click()
            clients.append((driver, driver.find_element_by_id('pid').text))

        yield self.wait_until(
            self._peer_connection_established,
            recv_driver=clients[0][0], requester_cid=clients[1][1])

        # Disconnect from client 0, which disconnects 0 and 1.
        clients[0][0].find_element_by_id('close').click()
        for driver in (clients[0][0], clients[1][0]):
            with self.assertRaises(NoSuchElementException):
                driver.find_element_by_class_name('peer')

        # request-partner from 0, which should connect to 2.
        clients[0][0].find_element_by_id('get-partner').click()

        yield self.wait_until(
            self._peer_connection_established,
            recv_driver=clients[2][0], requester_cid=clients[0][1])

        # Send a message from 2 to 0.
        chatbox = clients[2][0].find_element_by_id('text')
        chatbox.send_keys("message text", Keys.RETURN)
        # This times out sometimes. Maybe I need to drain the websocket
        # messages too?
        recv_message_from = WebDriverWait(clients[0][0], 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "from-peer"))
        )
        self.assertEqual(recv_message_from.text, 'Peer: message text')
        self.assertTrue('You: message text' in clients[2][0].page_source)
