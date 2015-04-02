import operator
import unittest

from bitarray import bitarray
import fakeredis
from mock import Mock, patch
from nose.plugins.attrib import attr

from instantdiscourse.app import ChatNode


@attr('unit')
class NodeTest(unittest.TestCase):
    capacity = 4
    error_rate = .08

    def _get_node_and_spy(self, redis_fake):
        redis_spy = Mock(wraps=redis_fake)
        node = ChatNode(redis_spy, self.capacity, self.error_rate)

        return node, redis_spy

    def setUp(self):
        self.redis_fake = fakeredis.FakeStrictRedis()
        ChatNode.zero_redis_filter(self.redis_fake, self.capacity, self.error_rate, force=True)
        self.node, self.redis_spy = self._get_node_and_spy(self.redis_fake)

    def test_get_unused_cid(self):
        self.assertNotEqual(self.node.get_unused_cid(), self.node.get_unused_cid())

        self.assertEqual(len(self.node.cids_allocated), 2)
        self.assertEqual(len(self.node.cids_in_use), 0)

    def test_cid_state_management(self):
        cid = self.node.get_unused_cid()
        self.assertEqual(len(self.node.cids_allocated), 1)

        self.node.set_cid_in_use(cid)
        self.assertEqual(len(self.node.cids_allocated), 0)
        self.assertEqual(len(self.node.cids_in_use), 1)

        self.node.remove_cid_in_use(cid)
        self.assertEqual(len(self.node.cids_allocated), 0)
        self.assertEqual(len(self.node.cids_in_use), 0)

    def test_hash_namespace(self):
        self.assertNotEqual(self.node._hash_key('foo'), 'foo')
        self.assertFalse(
            self.node.redis_filter_key.startswith(
                self.node.redis_hash_namespace))

    def test_filter_miss_believed_over_redis(self):
        self.assertFalse(self.node.has_been_said('not said yet'))
        self.node.record_as_said('has been said')

        # Force the filter to return False, while redis would return True.
        # We shouldn't hit redis, because the filter will never have false
        # negatives.
        with patch.object(self.node, 'message_filter', set()):
            self.assertFalse(self.node.has_been_said('has been said'))

        self.assertFalse(self.redis_spy.exists.called)

    def test_filter_hit_falls_back_to_redis(self):
        self.assertFalse(self.node.has_been_said('collision'))

        # Force the filter to return True for a message that hasn't been said.
        # The node should fall back to redis, which will definitively report
        # it has not been said.
        with patch.object(self.node, 'message_filter', set(['collision'])):
            self.assertTrue('collision' in self.node.message_filter)
            self.assertFalse(self.node.has_been_said('collision'))

        self.redis_spy.exists.assert_called_once_with(self.node._hash_key('collision'))

    def test_sync_filter_endianness(self):
        self.node.record_as_said('foo')
        old_bits = bitarray(endian='little')
        old_bits.frombytes(self.node.message_filter.bitarray.tobytes())
        self.node.sync_filter_from_redis()
        self.assertEqual(old_bits, self.node.message_filter.bitarray)

    def test_sync_filter_with_n_nodes(self):
        # Create n nodes and record unique messages among them.
        n = 5
        nodes = [self.node] + [self._get_node_and_spy(self.redis_fake)[0]
                               for i in xrange(n - 1)]
        for i, node in enumerate(nodes):
            node.record_as_said(str(i))

        # Check that their filters not all the same.
        # This would break the test later on, since it means
        # we wouldn't need to sync from redis in order to all be equal.
        filter_bits = [node.message_filter.bitarray
                       for node in nodes]
        self.assertFalse(
            all(b == filter_bits[0] for b in filter_bits),
            "All filters were equal: %r" % filter_bits)

        # Check that redis stores all of the filters ored together.
        expected_redis_bits = reduce(operator.or_, filter_bits)
        redis_bits = bitarray(endian='little')
        redis_bits.frombytes(self.redis_fake.get(self.node.redis_filter_key))
        self.assertEqual(
            expected_redis_bits, redis_bits,
            "Redis was %r, expected %r" % (redis_bits, expected_redis_bits))

        # Sync all the nodes and check that all nodes and redis agree.
        for node in nodes:
            node.sync_filter_from_redis()

        filter_bytes = [node.message_filter.bitarray.tobytes()
                        for node in nodes]
        filter_bytes.append(self.redis_fake.get(self.node.redis_filter_key))

        self.assertTrue(
            all(b == filter_bytes[0] for b in filter_bytes),
            "All filters were not equal: %r" % filter_bytes)
