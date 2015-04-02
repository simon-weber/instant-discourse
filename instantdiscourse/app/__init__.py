import logging
import uuid

from pybloom import BloomFilter


class ChatNode(object):
    # Not thread-safe.

    redis_filter_key = 'IDBF'
    redis_hash_namespace = 'ID-'

    @classmethod
    def zero_redis_filter(cls, redis, capacity, error_rate, force=False):
        # this should probably be a classmethod that nodes can call
        # with their values. lets us not instantiate one.
        node = cls(redis, capacity, error_rate, sync_now=False)
        node._overwrite_redis_filter(force=force)

    def __init__(self, redis, capacity, error_rate, sync_now=True):
        self.host_id = uuid.getnode()
        self.redis = redis
        self.message_filter = BloomFilter(capacity, error_rate)
        self.log = logging.getLogger("%s.%s" % (__name__, self.__class__.__name__))

        filter_bits = len(self.message_filter.bitarray)
        assert filter_bits % 8 == 0,\
            "Filter bits must be divisible by 8 (got %s)" % filter_bits

        self.cids_allocated = set()
        # id has been given out, but the client's not connected

        self.cids_in_use = set()
        # connected clients

        if sync_now:
            self.sync_filter_from_redis()

    def _overwrite_redis_filter(self, force=False):
        command = self.redis.setnx
        if force:
            command = self.redis.set

        command(
            self.redis_filter_key,
            self.message_filter.bitarray.tobytes())

    def _hash_key(self, hash):
        return self.redis_hash_namespace + hash

    def _get_cid(self):
        return str(uuid.uuid1(self.host_id))

    def get_unused_cid(self):
        cid = self._get_cid()
        self.cids_allocated.add(cid)
        return cid

    def remove_cid_in_use(self, cid):
        self.cids_in_use.remove(cid)

    def set_cid_in_use(self, cid):
        self.cids_allocated.remove(cid)
        self.cids_in_use.add(cid)

    def has_been_said(self, message):
        said = False

        if message in self.message_filter:
            self.log.info("said(%r): filter inconclusive; checking redis", message)
            said = self.redis.exists(self._hash_key(message))

        self.log.info("said(%r): %s", message, said)
        return said

    def record_as_said(self, message):
        self.log.info("recording %r", message)
        self.redis.set(self._hash_key(message), True)

        bits_set = self.message_filter.add_return_bits(message)
        self.log.debug("setting bits %s", bits_set)
        for bit_index in bits_set:
            # TODO pipeline these
            # The bitarray indices are significance-based,
            # but redis uses a raw offset.
            # There's probably a faster way to do this.
            # offset = (bit_index / 8 * 8) + (8 - (bit_index % 8) - 1)
            offset = bit_index - ((bit_index % 8) << 1) + 7
            self.redis.setbit(self.redis_filter_key, offset, 1)

    def sync_filter_from_redis(self):
        # The correctness of this operation depends on two things:
        #  1) record_as_said will never result in local bits set that are not
        #     set in redis. Redis failures will cause problems here!
        #  2) tornado is single threaded, so if record_as_said does not yield
        #     we can't run in the middle of it.
        redis_filter_bytes = self.redis.get(self.redis_filter_key)
        if redis_filter_bytes is None:
            raise ValueError("Tried to sync from redis, but redis has no filter yet.")

        filter_memory = memoryview(self.message_filter.bitarray)
        filter_memory[:] = redis_filter_bytes

        self.log.info("synced filter from redis")
