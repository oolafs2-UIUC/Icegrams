"""

    ngrams.py

    Copyright (C) 2019 Miðeind ehf.
    Original author: Vilhjálmur Þorsteinsson

    This module reads a tab-separated text file (.tsv file)
    containing trigrams and their frequency counts, and
    generates a compressed binary database of trigrams
    that can be queried for unigram, bigram and trigram
    frequencies.

    The trigram compression is based on the scheme described
    by G.E. Pibiri and R. Venturini (2017) in "Efficient Data Structures
    for Massive N-Gram Datasets", Proceedings of the 40th International
    ACM SIGIR Conference on Research and Development in Information
    Retrieval, pp. 615-624. The paper is available online at:
    http://pages.di.unipi.it/pibiri/papers/SIGIR17.pdf

    See also an updated (2018) version of the paper at:
    https://arxiv.org/pdf/1806.09447.pdf

    In summary, the scheme is broadly as follows:

    1) All distinct unigrams (tokens) are stored in a compressed
       Trie data structure, yielding a mapping unigram -> id,
       where the id is a 32-bit unsigned integer.
    2) Unigram ids are allocated in order of descending
       frequency of occurrence in the ngram list (note: not
       the actual unigram frequency in the source text).
       Thus, the unigram which occurs most often gets the
       lowest id. However, an exception is made for the empty
       (null) unigram which always has id 0 regardless of
       frequency.
    3) At the unigram level, we store a sequence of pointers
       to the child bigrams. Thus, if the unigram with id N
       leads into K (>=0) child bigrams, the unigram pointer list
       entry UP[N] will contain P and entry UP[N+1] will contain
       P+K. UP[0] is always 0. The UP list is monotonically
       increasing (although K=0 is allowed) and can be stored
       with Elias-Fano encoding.
    4) At the bigram level, we store an id list in increasing
       id order, with a prefix sum scheme. To all entries in a
       unigram's child list BI, spanning from BI[UP[N]] up to
       BI[UP[N+1]], we add the constant BI[UP[N]-1] (where
       BI[-1] is taken to be 0). This enables the id list to
       be encoded using Elias-Fano. We also store another pointer
       list into the trigram level, which is monotonically
       increasing and thus also Elias-Fano encodable.
    5) At the trigram level, instead of storing an id list as
       such, we use a trick from the cited paper. Each trigram
       is coded as (id0, id1, id2). This means that (id1, id2)
       is also stored as a bigram. Instead of storing id2 in
       the id list, we can store its index within id1's children
       in the unigram-to-bigram relation. This index is a much lower
       number than id2 itself and thus yields a significantly more
       compact Elias-Fano encoding.
    6) Frequency counts are stored at each level in a separate
       compressed hierarchical list. The frequencies that occur
       at each level are first counted and bucketed along with
       their frequency of occurrence. The buckets are then allocated
       code words where the most common buckets get the shortest
       code words. A separate bit array keeps track of where the
       code words start in the main array. This means that the
       frequency list is coded in 2N bits where N is the sum of
       the lengths of the code words used. The most common bucket
       numbers thus take only a couple of bits each.

"""

import time
from collections import defaultdict, deque
from bisect import bisect_left
from heapq import nlargest
import struct
import math
import io
import mmap
import functools

# Import the CFFI wrapper for the trie.cpp C++ module (see also build_trie.py)
if __package__:
    from ._trie import lib as trie_cffi, ffi
else:
    from _trie import lib as trie_cffi, ffi

# TSV_FILENAME = "trigrams.tsv"
TSV_FILENAME = "trigrams-subset.tsv"
# TSV_FILENAME = "3-grams.sorted"
# BINARY_FILENAME = "trigrams.bin"
BINARY_FILENAME = "trigrams-subset.bin"
# BINARY_FILENAME = "3-grams.sorted.bin"
UINT32 = struct.Struct("<I")
UINT16 = struct.Struct("<H")
UINT8 = struct.Struct("<B")

# Trigrams containing characters not in the following alphabet are rejected
ALPHABET = (
    "!$%'(),-./0123456789:;?ABCDEFGHIJKLMNOPQRSTUVWXYZ[]abcdefghijklmnopqrstuvwxyz"
    "°²³´µÀÁÄÅÆÉÍÐÓÖØÚÜÝÞßàáäåæçèéêëíîïðóôöøúüýþʹ‘’“”€"
)
# The alphabet can have a maximum of 126 characters due to compression restrictions
assert len(ALPHABET) < 2**7 - 1
ALPHABET_SET = set(ALPHABET)


def to_bytes(str):
    """ Convert string from normal Python representation to
        a bytes string containing indices into the alphabet.
        The indices are offset by 1 since 0 is not a valid
        byte value. """
    return bytes(ALPHABET.index(ch) + 1 for ch in str)


def to_str(by):
    """ Convert a sequence of byte indices into a normal Python string.
        The byte indices are decremented by 1 before the conversion,
        since 0 is not a valid byte index. """
    return "".join(ALPHABET[b - 1] for b in by)


class Ngrams:

    """ A wrapper class around the n-gram store, allowing
        queries for n-gram frequencies and probabilities.
        The current n-gram store contains unigrams, bigrams and
        trigrams. """

    def __init__(self):
        self.ngrams = NgramStorage()
        self.ngrams.load(BINARY_FILENAME)

    def freq(self, *args):
        """ Return the frequency of the n-gram given in *args, where
            1 <= n <= 3. The frequency is adjusted so that n-grams
            that do not occur in the database have frequency 1, and all
            others have their actual frequency incremented by one. """
        if not args:
            raise ValueError("Must provide at least one string argument")
        return self.ngrams.freq(*args)

    def logprob(self, *args):
        """ Return the log of the approximate probability
            of word w(n) given its predecessors w(1)..w(n-1),
            for 1 <= n <= 3 (i.e. unigram, bigram or trigram) """
        if not args:
            raise ValueError("Must provide at least one string argument")
        return self.ngrams.logprob(*args)

    def prob(self, *args):
        """ Return the approximate probability (in the range (0.0..1.0],
            note that it is never zero) of word w(n) given its
            predecessors w(1)..w(n-1), for 1 <= n <= 3 (i.e. unigram,
            bigram or trigram) """
        if not args:
            raise ValueError("Must provide at least one string argument")
        return math.exp(self.logprob(*args))

    def succ(self, n, *args):
        """ Returns a sorted list of length <= n with the most likely
            successors to the words given, in descending order of
            log probability. The list consists of tuples of
            (word, log probability). """
        if not isinstance(n, int):
            raise TypeError("Expected integer for parameter n")
        if not args:
            raise ValueError("Must provide at least one string argument")
        return self.ngrams.succ(n, *args)

    def close(self):
        """ Close the underlying storage and its memory map """
        self.ngrams.close()
        self.ngrams = None


class BitArray:

    """ BitArray implements a compressed array of bits.
        Bits are indexed starting from the least significant
        bit of each byte. Bit 0 is thus the lowest bit of
        the first byte of the array and bit 7 is the highest
        bit of that byte. """

    def __init__(self):
        # Accumulator for completed bytes
        self.b = bytearray()
        # The bits that have not been written to the byte array
        self.buf = 0
        # The number of bits currently in self.buf
        self.bits = 0
        # The total number of bits stored
        self.length = None

    def num_bits(self):
        """ Return the total number of bits written to the byte array """
        return len(self.b) * 8 + self.bits

    def append(self, val, bits):
        """ Append the given value to the BitArray, using the indicated
            number of bits. The value is masked by this function before
            adding it to the array. """
        assert self.length is None
        if bits <= 0:
            raise ValueError("Bits parameter must be > 0")
        # Add the new bits on the most significant side of the
        # current buffer
        self.buf |= ((val & ((1 << bits) - 1))) << self.bits
        self.bits += bits
        # Emit completed bytes, if any, from the least significant
        # side of the current buffer
        while self.bits >= 8:
            self.b.append(self.buf & 0xFF)
            self.buf >>= 8
            self.bits -= 8

    def finish(self):
        """ Optionally call this to complete writing any still
            buffered bits to the byte array """
        assert self.length is None
        self.length = len(self.b) * 8 + self.bits
        if self.bits:
            assert self.bits < 8
            assert self.buf < 0x80
            self.b.append(self.buf)
            self.buf = 0
            self.bits = 0

    def get(self, index, bits):
        """ Obtain the value stored at the given bit index, using
            the indicated number of bits """
        if bits <= 0:
            raise ValueError("Bits parameter must be > 0")
        # Finish writing to the byte buffer
        if self.length is None:
            self.finish()
        if index + bits > self.length:
            raise IndexError("Attempt to index beyond end of BitArray")
        # Find out which byte the value starts in
        by = index >> 3
        # Find out which bit the value starts in
        index &= 0x07
        # Get as many bits as we can out of the first byte
        buf = self.b[by] >> index
        # This is how many bits we've now got in our buffer
        bufbits = 8 - index
        while bufbits < bits:
            # Not enough bufbits yet: move to the next byte
            by += 1
            if bufbits + 8 <= bits:
                # Get all 8 bufbits from it
                buf |= self.b[by] << bufbits
                bufbits += 8
            else:
                # Get fewer bufbits
                rest = bits - bufbits
                buf |= (self.b[by] & ((1 << rest) - 1)) << bufbits
                bufbits += rest
                assert bufbits == bits
        return buf & ((1 << bits) - 1)

    def to_bytes(self):
        """ Finish the byte array and return it as a bytes object """
        if self.length is None:
            self.finish()
        return bytes(self.b)

    def __len__(self):
        """ Return the length of this BitArray, in bytes """
        return len(self.b) + (1 if self.bits else 0)


class BaseList:

    def lookup(self, ix):
        """ Should always be overridden in derived classes """
        raise NotImplementedError

    def __getitem__(self, ix):
        """ Returns the integer at index ix within the sequence """
        return self.lookup(ix)

    def lookup_pair(self, ix):
        """ Return the pair of values at [ix] and [ix+1] """
        # !!! TODO: Optimize this
        return (self.lookup(ix), self.lookup(ix + 1))

    def search_prefix(self, p1, p2, i):
        """ Binary search for the identifier i in the range [p1, p2> """
        # In this case an identifier list id0, id1, ..., idN was stored
        # using a prefix sum, which is the value of the preceding
        # identifier entry in the list (i.e. id(-1)). The prefix
        # sum was added to all entries in the list, so we add it
        # to i before doing a normal binary search.
        if p1 >= p2:
            return None
        if p1 > 0:
            # The prefix sum of element 0 is 0
            i += self.lookup(p1 - 1)
        return self.search(p1, p2, i)

    def search(self, p1, p2, i):
        """ Binary search for the identifier i in the range [p1, p2> """
        while True:
            if p1 >= p2:
                return None
            mid = (p1 + p2) // 2
            this_id = self.lookup(mid)
            if this_id == i:
                return mid
            if this_id > i:
                # Too high: lower our roof
                p2 = mid
            else:
                # Too low (this_id < i): raise our floor
                p1 = mid + 1


class MonotonicList(BaseList):

    """ A MonotonicList stores a presorted, monotonically increasing
        list of integers in a compact byte buffer using Elias-Fano
        encoding. """

    QUANTUM_SIZE = 128

    def __init__(self, b=None):
        # If b is given, it should be a byte buffer of some sort
        # (usually a memoryview() object)
        self.b = b
        self.ffi_b = None if b is None else ffi.from_buffer(b)
        self.n = 0
        self.u = 0
        self.low_bits = 0
        self.high_bits = 0

    def compress(self, int_list, vocab_size=None):
        """ Compress a presorted, monotonically increasing list of integers
            in int_list, all of them <= vocab_size (if given), to a bytes() object
            and return it """
        self.n = n = len(int_list)
        if n == 0 or n >= 2 ** 32:
            raise ValueError("List must have more than zero and less than 2**32 elements")

        # Get vocabulary size
        if vocab_size is None:
            self.u = u = int_list[-1]
        else:
            assert vocab_size >= int_list[-1]
            self.u = u = vocab_size
        if u == 0:
            # Degenerate case
            self.low_bits = low_bits = 1
            self.high_bits = high_bits = 0
        else:
            self.low_bits = low_bits = max(1, int(math.log(u / n, 2)))
            self.high_bits = high_bits = max(0, int(math.log(u, 2) + 1.0) - low_bits)
        low_mask = (1 << low_bits) - 1
        # Prepare the compressed buffer, low bits part
        buf = bytearray()
        # Prepare the compressed buffer, high bits part
        high_size = n + (u >> low_bits)
        hbuf = bytearray((high_size + 7) >> 3)
        # Prepare the quantized index into the high bits buffer,
        # with 2048 bits in each quantum
        hbuf_index = bytearray()
        # Accumulator for the low bits
        low_buf = 0
        # Number of bits in low_buf
        low_cnt = 0
        last_item = 0
        # The index of the last bit written to the high bit buffer
        hbit = 0

        # Main encoding loop
        for ix, item in enumerate(int_list):
            # Check for monotonicity
            assert item >= last_item
            # Check that the item is inside the vocabulary
            assert item <= u
            # First, add the low bits of the item to the low
            # bits array, starting with the least significant bit
            # and working upwards towards more significant bits
            low_buf |= (item & low_mask) << low_cnt
            # Keep count of the significant bits we have
            low_cnt += low_bits
            while low_cnt >= 8:
                # We have accumulated 8 or more significant bits:
                # emit them, from the left (least significant) side of the bit buffer
                buf.append(low_buf & 0xFF)
                low_buf >>= 8
                low_cnt -= 8
            # Note that item is monotonically increasing between steps. Therefore
            # its high part increases at each step by >= 0. Of course, ix increases
            # by 1 at each step, and we set an hbit at the index
            # (high part + ix) to 1, but we never set the same hbit twice.
            # The only time we skip an 1 - and thus leave a 0 - is when
            # the high part increments by (at least) 1.
            # The number of zeroes that we find on our way to the n-th 1
            # (n being 0-based) is thus the value of the high part for the
            # item at index n.
            if high_bits > 0:
                if ix % self.QUANTUM_SIZE == 0 and ix:
                    # At index intervals of QUANTUM_SIZE,
                    # store where we were in the hbuf before
                    # writing this list item
                    assert hbit < 1 << 32
                    hbuf_index += UINT32.pack(hbit + 1)
                hbit = (item >> low_bits) + ix
                # Set the bit with index (i + high)
                hbuf[hbit >> 3] |= 1 << (hbit & 0x07)
            # And remember the last id
            last_item = item

        # Complete the low part
        if low_cnt > 0:
            # Output any remaining bits on the left hand side of the
            # final byte
            assert low_buf <= 0xFF
            buf.append(low_buf)

        # Construct the final compressed buffer
        # Add an 8-byte header in front containing n and the number of low bits,
        # which is all we need for decompression
        if high_bits > 0:
            try:
                assert len(hbuf_index) == ((self.n - 1) // self.QUANTUM_SIZE) * 4
            except AssertionError:
                print("len(hbuf_index) is {0}, self.n is {1}, QUANTUM_SIZE is {2}"
                    .format(len(hbuf_index), self.n, self.QUANTUM_SIZE))
                raise
        parts = [
            UINT32.pack(self.n),
            UINT16.pack(low_bits), UINT16.pack(high_bits),
            bytes(hbuf_index),
            bytes(buf + hbuf)
        ]
        # Align the byte block to a DWORD (32-bit) boundary
        frag = sum(len(p) for p in parts) & 3
        if frag:
            parts.append(b"\x00" * (4 - frag))
        self.b = b"".join(parts)
        self.ffi_b = ffi.from_buffer(self.b)

    def to_bytes(self):
        """ Return a bytes object containing the compressed list """
        return self.b

    def __str__(self):
        s = "MonotonicList: u is {0:,}, n is {1:,}\n".format(self.u, self.n)
        s += (
            "low_bits is {0}, high_bits is {1}, total range {2:,}\n"
            .format(self.low_bits, self.high_bits, 2**(self.low_bits + self.high_bits) - 1)
        )
        s += (
            "size in bytes is {0:,} instead of straightforward {1:,}"
            .format(len(self.b), (self.n * int(math.log(self.u, 2) + 1.0) + 7) // 8)
        )
        return s

    def __len__(self):
        """ Return the number of elements in the list """
        return self.n

    def lookup(self, ix):
        """ Returns the integer at index ix within the sequence """
        if self.ffi_b is None:
            raise ValueError("Lookup not allowed from uncompressed list")
        return trie_cffi.lookupMonotonic(self.ffi_b, self.QUANTUM_SIZE, ix)


class PartitionedMonotonicList(BaseList):

    """ A PartitionedMonotonicList consists of a list
        of Elias-Fano lists, with the trick being that
        each sublist is encoded with its own item
        sequence, after subtracting the value of the
        first item of the list (which is stored in
        the first level list). """

    QUANTUM_SIZE = 1 << 11

    def __init__(self, b=None):
        self.b = b
        self.ffi_b = None if b is None else ffi.from_buffer(b)

    def compress(self, int_list):
        """ Compress int_list into a two-level partitioned
            Elias-Fano list, where the lower level consists
            of sublists of length <= QUANTUM_SIZE, and the
            upper level consists of a list of the values of
            the first items of the sublists. """

        # The upper level list
        chunks = []
        # The byte offsets of the lower-level lists
        chunk_index = [0]
        # The current prefix to be subtracted from each
        # sublist item, i.e. the value of the first
        # item in the current sublist
        prefix = 0
        # The current sublist
        sq = []
        # The accumulated compressed sublists
        buf = []
        # The number of sublist bytes accumulated so far
        buf_size = 0
        # The compressor object
        ml = MonotonicList()
        Q = self.QUANTUM_SIZE

        for ix, item in enumerate(int_list):
            if ix % Q == 0 and ix:
                # Finishing a sublist and starting a new one:
                # note the value of the first item in the
                # new sublist
                chunks.append(item)
                # Switch to a new prefix value to subtract
                prefix = item
                # Compress the previous sublist and append
                # its byte buffer to our accumulator list
                ml.compress(sq)
                b = ml.to_bytes()
                buf.append(b)
                # Add to the byte offset
                buf_size += len(b)
                # Note the byte offset of the new sublist
                chunk_index.append(buf_size)
                # Start a new sublist
                sq = []
            # Add the item to the current sublist, after
            # subtracting the prefix value
            assert item >= prefix
            sq.append(item - prefix)

        if sq:
            # Clean up remaining items in the current sublist
            ml.compress(sq)
            b = ml.to_bytes()
            buf.append(b)
            buf_size += len(b)

        # Create a merged buffer of all accumulated sublists
        buf = b"".join(buf)
        # Compress the upper level list
        ml.compress(chunks)
        chunk_bytes = ml.to_bytes()
        # Calculate the offset of the upper level list within
        # the resulting byte buffer
        offset = 4 + 4 * len(chunk_index) + len(chunk_bytes)
        # Assemble the final byte buffer
        parts = [
            UINT32.pack(len(chunk_index)),
            b"".join(UINT32.pack(pos + offset) for pos in chunk_index),
            chunk_bytes,
            buf
        ]
        # Align the byte block to a DWORD (32-bit) boundary
        frag = sum(len(p) for p in parts) & 3
        if frag:
            parts.append(b"\x00" * (4 - frag))
        self.b = b"".join(parts)
        self.ffi_b = ffi.from_buffer(self.b)
        print(
            "Completed compression of PartitionedMonotonicList, sublists are {0:,}"
            .format(len(chunks))
        )

    def to_bytes(self):
        """ Return the byte buffer containing the compressed list """
        return self.b

    def lookup(self, ix):
        """ Lookup a value from the compressed list, by index """
        if self.ffi_b is None:
            raise ValueError("Lookup not allowed from uncompressed list")
        return trie_cffi.lookupPartition(
            self.ffi_b, self.QUANTUM_SIZE, MonotonicList.QUANTUM_SIZE, ix
        )


class _Node:

    """ A Node within a Trie """

    __slots__ = ("fragment", "value", "children")

    def __init__(self, fragment, value):
        # The key fragment that leads into this node (and value)
        self.fragment = fragment
        self.value = value
        # List of outgoing nodes
        self.children = None

    def add(self, fragment, value):
        """ Add the given remaining key fragment to this node """
        if len(fragment) == 0:
            if self.value is not None:
                # This key already exists: return its value
                return self.value
            # This was previously an internal node without value;
            # turn it into a proper value node
            self.value = value
            return None

        if self.children is None:
            # Trivial case: add an only child
            self.children = [_Node(fragment, value)]
            return None

        # Check whether we need to take existing child nodes into account
        lo = 0
        hi = len(self.children)
        ch = fragment[0]
        while hi > lo:
            mid = (lo + hi) // 2
            mid_ch = self.children[mid].fragment[0]
            if mid_ch < ch:
                lo = mid + 1
            elif mid_ch > ch:
                hi = mid
            else:
                break

        if hi == lo:
            # No common prefix with any child:
            # simply insert a new child into the sorted list
            # if lo > 0:
            #     assert self._children[lo - 1]._fragment[0] < fragment[0]
            # if lo < len(self._children):
            #     assert self._children[lo]._fragment[0] > fragment[0]
            self.children.insert(lo, _Node(fragment, value))
            return None

        assert hi > lo
        # Found a child with at least one common prefix character
        # noinspection PyUnboundLocalVariable
        child = self.children[mid]
        child_fragment = child.fragment
        # assert child_fragment[0] == ch
        # Count the number of common prefix characters
        common = 1
        len_fragment = len(fragment)
        len_child_fragment = len(child_fragment)
        while (
            common < len_fragment
            and common < len_child_fragment
            and fragment[common] == child_fragment[common]
        ):
            common += 1
        if common == len_child_fragment:
            # We have 'abcd' but the child is 'ab':
            # Recursively add the remaining 'cd' fragment to the child
            return child.add(fragment[common:], value)
        # Here we can have two cases:
        # either the fragment is a proper prefix of the child,
        # or the two diverge after #common characters
        # assert common < len_child_fragment
        # assert common <= len_fragment
        # We have 'ab' but the child is 'abcd',
        # or we have 'abd' but the child is 'acd'
        child.fragment = child_fragment[common:]  # 'cd'
        if common == len_fragment:
            # The fragment is a proper prefix of the child,
            # i.e. it is 'ab' while the child is 'abcd':
            # Break the child up into two nodes, 'ab' and 'cd'
            node = _Node(fragment, value)  # New parent 'ab'
            node.children = [child]  # Make 'cd' a child of 'ab'
        else:
            # The fragment and the child diverge,
            # i.e. we have 'abd' but the child is 'acd'
            new_fragment = fragment[common:]  # 'bd'
            # Make an internal node without a value
            node = _Node(fragment[0:common], None)  # 'a'
            # assert new_fragment[0] != child._fragment[0]
            if new_fragment[0] < child.fragment[0]:
                # Children: 'bd', 'cd'
                node.children = [_Node(new_fragment, value), child]
            else:
                node.children = [child, _Node(new_fragment, value)]
        # Replace 'abcd' in the original children list
        self.children[mid] = node
        return None

    def lookup(self, fragment):
        """ Lookup the given key fragment in this node and its children
            as necessary """
        if not fragment:
            # We've arrived at our destination: return the value
            return self.value
        if self.children is None:
            # Nowhere to go: the key was not found
            return None
        # Note: The following could be a faster binary search,
        # but this lookup is not used in time critical code,
        # so the optimization is probably not worth it.
        for child in self.children:
            if fragment.startswith(child.fragment):
                # This is a continuation route: take it
                return child.lookup(fragment[len(child.fragment):])
        # No route matches: the key was not found
        return None

    def __str__(self):
        s = "Fragment: '{0}', value '{1}'\n".format(self.fragment, self.value)
        c = ["   {0}".format(child) for child in self.children] if self.children else []
        return s + "\n".join(c)


class Trie:

    """ Wrapper class for a radix (compact) trie data structure.
        Each node in the trie contains a prefix string, leading
        to its children. """

    def __init__(self, root_fragment=b"", reserve_zero_for_empty=True):
        # We reserve the 0 index for the empty string
        self._cnt = 1 if reserve_zero_for_empty else 0
        self._root = _Node(root_fragment, None)

    @property
    def root(self):
        """ Return the root node of the trie """
        return self._root

    def add(self, key, value=None):
        """ Add the given (key, value) pair to the trie.
            Duplicates are not allowed and not added to the trie.
            If the value is None, it is set to the number of entries
            already in the trie, thereby making it function as
            an automatic generator of list indices. """
        if not key:
            return 0
        if value is None:
            value = self._cnt
        prev_value = self._root.add(key, value)
        if prev_value is not None:
            # The key was already found in the trie: return the
            # corresponding value
            return prev_value
        # Not already in the trie: add to the count and return the new value
        self._cnt += 1
        return value

    def get(self, key, default=None):
        """ Lookup the given key and return the associated value,
            or the default if the key is not found. """
        if not key:
            return 0
        value = self._root.lookup(key)
        return default if value is None else value

    def __getitem__(self, key):
        """ Lookup in square bracket notation """
        if not key:
            return 0
        value = self._root.lookup(key)
        if value is None:
            raise KeyError(key)
        return value

    def __len__(self):
        """ Return the number of unique keys within the trie,
            including the empty string sentinel that has the value 0 """
        return self._cnt


class _Level:

    """ A level within a trigram tree structure """

    __slots__ = ("cnt", "d")

    def __init__(self, depth):
        self.reset(depth)

    def reset(self, depth):
        self.cnt = 0
        if depth >= NgramStorage.MAX_N:
            self.d = None
        else:
            self.d = defaultdict(lambda: _Level(depth + 1))


class NgramStorage:

    """ NgramStorage wraps the compressed binary representation of
        the trigram store """

    # We store unigrams, bigrams and trigrams
    MAX_N = 3
    # We store an index position in the frequency array once
    # every FREQ_QUANTUM_SIZE frequency values
    FREQ_QUANTUM_SIZE = 1024

    VERSION = b'Reynir 001.00.00'
    assert len(VERSION) == 16

    def __init__(self):
        self.trie = None
        self.log_ucnt = 0.0
        # A dictionary of frequency buckets in ascending order
        self.freqs = None
        # Level 0 of the trigram tree
        self.level0 = None
        self.vocab_size = 0
        # Memory mapped binary buffer
        self._b = None
        self._mmap_buffer = None

    def compress(self, tsv_filename, binary_filename, *, add_all_bigrams=False):
        """ Create a new compressed binary file from a trigram text (.tsv) file.
            If add_all_bigrams is True, then for each input trigram (w0, w1, w2)
            we add both (w0, w1) and (w1, w2) as bigrams. Otherwise, we add only
            (w0, w1) - and assume that (w1, w2, w3) is also present as a trigram
            causing (w1, w2) to be implicitly added. """
        self.read_tsv(tsv_filename, add_all_bigrams=add_all_bigrams)
        self.write_binary(binary_filename)

    def word_to_id(self, word):
        """ Obtain the unigram id for the given word by
            calling the C++ mapping() function from
            trie.cpp that has been wrapped using CFFI """
        if word == "":
            return 0
        try:
            m = trie_cffi.mapping(self._mmap_buffer, to_bytes(word))
        except ValueError:
            # The word contains a character that is not in our alphabet
            return None
        return None if m == 0xFFFFFFFF else m

    def indices(self, *args):
        """ Convert word strings to vocabulary indices, or None
            if the word is not found in the vocabulary """
        return tuple(self.word_to_id(w) for w in args)

    def lookup_frequency(self, level, b, index):
        """ Look up the frequency with the given index,
            stored in the byte buffer b """
        if index is None:
            return 0
        buf = ffi.from_buffer(b)
        rank = trie_cffi.lookupFrequency(buf, self.FREQ_QUANTUM_SIZE, index)
        # ...and finally retrieve the actual frequency
        return self.freqs[level][rank]

    def unigram_frequency(self, i0):
        """ Return the adjusted frequency of the unigram i0,
            specified as a vocabulary index. """
        return self.lookup_frequency(1, self._unigram_freqs, i0) + 1

    def unigram_logprob(self, i0):
        """ Return the log of the probability of the unigram
            given by vocabulary index i0, relative to the entire
            unigram frequency count """
        return math.log(self.unigram_frequency(i0)) - self.log_ucnt

    def bigram_frequency(self, i0, i1):
        """ Return the adjusted frequency of the bigram (i0, i1),
            given as vocabulary indices. """
        # Look up the pointer range for i0 in the unigram pointers
        if i0 is None or i1 is None:
            return 1
        # Check degenerate case
        if not (i0 or i1):
            return 1
        p1, p2 = self._unigram_ptrs_ml.lookup_pair(i0)
        # Then, look for id i1 within the level 2 ids delimited by [p1, p2>
        i = self._bigram_ml.search_prefix(p1, p2, i1)
        return self.lookup_frequency(2, self._bigram_freqs, i) + 1

    def bigram_logprob(self, i0, i1):
        """ Return the log of the probability of the bigram
            consisting of vocabulary indices i0 and i1,
            relative to the unigram frequency of i0 """
        return (
            math.log(self.bigram_frequency(i0, i1))
            - math.log(self.unigram_frequency(i0))
        )

    def trigram_frequency(self, i0, i1, i2):
        """ Return the adjusted frequency of the trigram (i0, i1, i2),
            given as vocabulary indices. """
        # Look up the pointer range for i0 in the unigram pointers
        if i0 is None or i1 is None or i2 is None:
            return 1
        # Check degenerate cases
        if not (i0 or i1 or i2):
            # This is (0, 0, 0)
            return 1
        if not (i0 or i1):
            # This is (0, 0, w2): lookup (0, w2) instead
            return self.bigram_frequency(i1, i2)
        if not (i1 or i2):
            # This is (w2, 0, 0): lookup (w2, 0) instead
            return self.bigram_frequency(i0, i1)
        p1, p2 = self._unigram_ptrs_ml.lookup_pair(i0)
        # Then, look for id i1 within the level 2 ids delimited by [p1, p2>
        i = self._bigram_ml.search_prefix(p1, p2, i1)
        if i is None:
            # Not found
            return 1
        p1, p2 = self._bigram_ptrs_ml.lookup_pair(i)
        if p1 >= p2:
            return 1
        # Apply the Pibiri & Venturini trick:
        # Remap i2 to an index within the list of children of i1
        q1, q2 = self._unigram_ptrs_ml.lookup_pair(i1)
        remapped_id = self._bigram_ml.search_prefix(q1, q2, i2)
        if remapped_id is None:
            # This can happen if (i0, i1) is present but (i1, i2)
            # is not. In this case, (i0, i1, i2) is not found.
            return 1
        i = self._trigram_ml.search_prefix(p1, p2, remapped_id - q1)
        return self.lookup_frequency(3, self._trigram_freqs, i) + 1

    def trigram_logprob(self, i0, i1, i2):
        """ Return the log of the probability of the trigram
            consisting of vocabulary indices i0, i1 and i2,
            relative to the bigram of i0 and i1 """
        return (
            math.log(self.trigram_frequency(i0, i1, i2))
            - math.log(self.bigram_frequency(i0, i1))
        )

    _FREQ_DISPATCH = {1: unigram_frequency, 2:bigram_frequency, 3:trigram_frequency}

    def freq(self, *args):
        """ Return the frequency of the n-gram given in *args, where
            1 <= n <= 3. The frequency is adjusted so that n-grams
            that do not occur in the database have frequency 1, and all
            others have their actual frequency incremented by one. """
        if len(args) > self.MAX_N:
            # Allow more than 3 arguments, but then we only return the
            # trigram probability of the last 3
            args = args[-self.MAX_N:]
        return self._FREQ_DISPATCH[len(args)](self, *self.indices(*args))

    _PROB_DISPATCH = {1: unigram_logprob, 2:bigram_logprob, 3:trigram_logprob}

    def logprob(self, *args):
        """ Return the log of the approximate probability
            of word w(n) given its predecessors w(1)..w(n-1),
            for 1 <= n <= 3 (i.e. unigram, bigram or trigram) """
        if len(args) > self.MAX_N:
            # Allow more than 3 arguments, but then we only return the
            # trigram probability of the last 3
            args = args[-self.MAX_N:]
        return self._PROB_DISPATCH[len(args)](self, *self.indices(*args))

    def unigram_succ(self, n, i0):
        """ Return successors to the unigram whose id is in i0 """
        if i0 is None:
            return []
        p1, p2 = self._unigram_ptrs_ml.lookup_pair(i0)
        if p1 >= p2:
            return []
        lp0 = math.log(self.lookup_frequency(1, self._unigram_freqs, i0) + 1)
        result = []
        prefix_sum = 0 if p1 is 0 else self._bigram_ml.lookup(p1 - 1)
        for i in range(p1, p2):
            j = self._bigram_ml.lookup(i) - prefix_sum
            lpi = math.log(self.lookup_frequency(2, self._bigram_freqs, i) + 1)
            result.append((j, lpi - lp0))
        return sorted(result, key=lambda e:e[1], reverse=True)[0:n]

    def bigram_succ(self, n, i0, i1):
        """ Return successors to the bigram (i0, i1) """
        if i0 is None or i1 is None:
            return []
        p1, p2 = self._unigram_ptrs_ml.lookup_pair(i0)
        if p1 >= p2:
            return []
        i = self._bigram_ml.search_prefix(p1, p2, i1)
        if i is None:
            # Not found
            return []
        p1, p2 = self._bigram_ptrs_ml.lookup_pair(i)
        if p1 >= p2:
            return []
        # Cache the bigram range of i1
        q1, q2 = self._unigram_ptrs_ml.lookup_pair(i1)
        prefix_sum_bi = self._bigram_ml.lookup(q1 - 1) if q1 > 0 else 0
        # Cache the bigram frequency of (i0, i1)
        lp0 = math.log(self.lookup_frequency(2, self._bigram_freqs, i) + 1)
        result = []
        prefix_sum_tri = self._trigram_ml.lookup(p1 - 1) if p1 > 0 else 0
        for i in range(p1, p2):
            # trigram[i] is a remapped id, i.e. it's an offset
            # into the bigram children of i1
            remapped_id = self._trigram_ml.lookup(i) - prefix_sum_tri
            j = self._bigram_ml.lookup(q1 + remapped_id) - prefix_sum_bi
            lpi = math.log(self.lookup_frequency(3, self._trigram_freqs, i) + 1)
            result.append((j, lpi - lp0))
        return sorted(result, key=lambda e:e[1], reverse=True)[0:n]

    _SUCC_DISPATCH = {1: unigram_succ, 2:bigram_succ}

    def succ(self, n, *args):
        """ Return a list of likely successors to the words
            in *args, of length <= n. The list consists of
            tuples of (word, log probability), in descending
            order of log probability. """
        if len(args) >= self.MAX_N:
            args = args[-(self.MAX_N - 1):]
        return self._SUCC_DISPATCH[len(args)](self, n, *self.indices(*args))

    def read_tsv(self, fname, *, add_all_bigrams=False):
        """ Populate the trigram database from a tab-separated (.tsv) file """
        print("Reading {0}, first pass...".format(fname), flush=True)
        t0 = time.time()

        # First pass: obtain the unigram vocabulary and count how many
        # times each unigram occurs. Note that this number is not the
        # same as the n-gram frequency count.
        vocab = defaultdict(int)
        cnt = 0
        with open(fname, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                cnt += 1
                if line:
                    a = line.split()
                    if len(a) != 4:
                        a = line.split("\t")
                    if len(a) != 4:
                        print("Error in line {0}: '{1}' {2}".format(cnt, line, a))
                    assert len(a) == 4
                    w0, w1, w2 = a[0:3]
                    charset = set(w0 + w1 + w2)
                    if not charset.issubset(ALPHABET_SET):
                        # Ignore trigrams that contain out-of-alphabet code points,
                        # such as Russian or Arabic characters
                        # print("Skipping line {0}".format(line))
                        continue
                    # Note that in this pass, we are counting occurrences,
                    # not frequency
                    vocab[to_bytes(w0)] += 1
                    vocab[to_bytes(w1)] += 1
                    vocab[to_bytes(w2)] += 1
        # Trie that maps unigrams to integer identifiers
        using_empty = b"" in vocab
        trie = Trie(reserve_zero_for_empty=using_empty)
        # Dict to map words to integer ids
        ids = { b"": 0 } if using_empty else {}
        # Build the trie in decreasing order of occurrences, ensuring that
        # the most common unigrams get the lowest indices
        if using_empty:
            # Hack to make sure that the blank entry goes to the front of the list
            vocab[b""] = 10**50
        vocab_list = sorted(vocab.items(), key=lambda item: item[1], reverse=True)
        assert not using_empty or vocab_list[0][0] == b""
        vocab = None
        for unigram_id, (w, c) in enumerate(vocab_list):
            if unigram_id == 0 and w == b"":
                # If the empty string is present, it is only allowed as id 0
                pass
            else:
                assert w
                trie_ix = trie.add(w)
                # Make sure that everything is synced up
                assert trie_ix == unigram_id
                ids[w] = trie_ix
        vocab_list = None

        # Second pass: index the trigrams
        # Line count
        cnt = 0
        # Instantiate the top (unigram) level of the trigram tree
        level0 = _Level(0)
        # Unigram frequencies and bigram pointers
        uf = level0.d
        # Total unigram frequency count
        ucnt = 0
        print("Reading {0}, second pass...".format(fname), flush=True)
        with open(fname, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                if line:
                    cnt += 1
                    a = line.split()
                    if len(a) != 4:
                        a = line.split("\t")
                    if len(a) != 4:
                        print("Error in line {0}: '{1}' {2}".format(cnt, line, a))
                    assert len(a) == 4
                    w0, w1, w2 = a[0:3]
                    charset = set(w0 + w1 + w2)
                    if not charset or not charset.issubset(ALPHABET_SET):
                        # Ignore trigrams that contain out-of-alphabet code points,
                        # such as Russian or Arabic characters
                        # print("Skipping line {0}".format(line))
                        continue
                    i0, i1, i2 = ids[to_bytes(w0)], ids[to_bytes(w1)], ids[to_bytes(w2)]
                    c = int(a[3])

                    # Note that in our trigram database, the sentence
                    # w0 w1 w2 w3
                    # will appear in the data as follows:
                    # (0, 0, w0)
                    # (0, w0, w1)
                    # (w0, w1, w2)
                    # (w1, w2, w3)
                    # (w2, w3, 0)
                    # (w3, 0, 0)
                    # To avoid counting the same unigram and bigram appearances
                    # multiple times, we only add to the unigram count for the
                    # first item in each triple, and to the bigram counts for the
                    # first and second item in each triple.

                    # Unigram frequency
                    d = uf[i0]
                    d.cnt += c
                    # Bigram frequency
                    d = d.d[i1]
                    d.cnt += c
                    # Trigram frequency
                    d = d.d[i2]
                    d.cnt += c

                    if add_all_bigrams:
                        # If the data does not contain (w1, w2, w3)
                        # for every (w0, w1, w2), add (w1, w2) explicitly
                        # Unigram frequency
                        d = uf[i2]
                        d.cnt += c
                        d = uf[i1]
                        d.cnt += c
                        # Bigram frequency
                        d = d.d[i2]
                        d.cnt += c
                        # In this case, we count each unigram separately
                        ucnt += 3 * c
                    else:
                        # Sum up all the unigram counts
                        ucnt += c

        if using_empty:
            # Save space by storing the counts of (0, 0, w2) in
            # (0, w2) and deleting (0, 0, w2)
            d0 = uf[0].d
            d00 = d0[0].d
            for w2 in d00.keys():
                d0[w2].cnt = d00[w2].cnt
            cut = len(d00)
            d0[0].reset(2)
            # Save space by storing the counts of (w0, 0, 0) in
            # (w0, 0) and deleting (w0, 0, 0)
            for w0 in uf.keys():
                if 0 in uf[w0].d:
                    w0d = uf[w0].d[0]
                    if 0 in w0d.d:
                        w0d.cnt = w0d.d[0].cnt
                        del w0d.d[0]
                        cut += 1
            print("Cut out {0:,} trigrams with two blanks".format(cut))

        self.trie = trie
        self.vocab_size = len(trie)
        level0.cnt = ucnt
        self.level0 = level0
        # The +1 below is intended to serve as a placeholder for
        # the long tail (out of vocabulary) unigrams, which are assigned
        # a frequency of 1, but admittedly it is there for OCD reasons
        # rather than strict necessity.
        self.log_ucnt = math.log(ucnt + 1)

        # Collect frequency buckets
        freqs = defaultdict(lambda: set())

        def count_level(depth, level):
            freqs[depth].add(level.cnt)
            if level.d:
                for _, v in level.d.items():
                    count_level(depth + 1, v)

        count_level(0, level0)
        # At the unigram level, for the test data, 0 is an allowed frequency
        freqs[1].add(0)
        for k, v in freqs.items():
            print("Level {0}: Frequency buckets are {1}".format(k, len(v)))
        # For each level, create a dict of indices into an ascending list of frequencies
        self.freqs = {
            k: { f: ix for ix, f in enumerate(sorted(list(v))) }
            for k, v in freqs.items()
        }

        t1 = time.time()
        print(
            "Done in {3:.1f} sec, trigram count is {0:,}, "
            "voc size is {1:,}, unigram count {2:,}"
            .format(cnt, len(trie), ucnt, t1 - t0)
        )

    def write_trie(self, f, trie):
        """ Write the unigram trie contents to a packed binary stream """
        # We assume that the alphabet can be represented in 7 bits
        todo = deque()
        node_cnt = 0
        single_char_node_count = 0
        multi_char_node_count = 0
        no_child_node_count = 0
        max_distance = 0

        def write_node(node, parent_loc):
            """ Write a single node to the packed binary stream,
                and fix up the parent's pointer to the location
                of this node """
            loc = f.tell()
            val = 0x007FFFFF if node.value is None else node.value
            assert val < 2**23
            nonlocal node_cnt, single_char_node_count, multi_char_node_count
            nonlocal no_child_node_count
            node_cnt += 1
            childless_bit = 0 if node.children else 0x40000000
            if len(node.fragment) <= 1:
                # Single-character fragment:
                # Pack it into 32 bits, with the high bit
                # being 1, the childless bit following it,
                # the fragment occupying the next 7 bits,
                # and the value occupying the remaining 23 bits
                if len(node.fragment) == 0:
                    chix = 0
                else:
                    chix = node.fragment[0]
                    assert chix != 0
                assert chix < 2**7
                f.write(
                    UINT32.pack(
                        0x80000000
                        | childless_bit
                        | (chix << 23)
                        | (val & 0x007FFFFF)
                    )
                )
                single_char_node_count += 1
                b = None
            else:
                # Multi-character fragment:
                # Store the value first, in 32 bits, and then
                # the fragment bytes with a trailing zero, padded to 32 bits
                f.write(UINT32.pack(childless_bit | (val & 0x007FFFFF)))
                b = node.fragment
                multi_char_node_count += 1
            # Write the child nodes, if any
            if node.children:
                f.write(UINT16.pack(len(node.children)))
                # !!! NOTE: The following could be compressed to
                # !!! a single child pointer, with a corresponding
                # !!! increase in complexity in the lookup algorithm,
                # !!! since the children are guaranteed to be
                # !!! consecutive in the file.
                for child in node.children:
                    todo.append((child, f.tell()))
                    # Write a placeholder - will be overwritten
                    f.write(UINT32.pack(0xFFFFFFFF))
            else:
                no_child_node_count += 1
            if b is not None:
                # The 0H aligns to 16 bits
                # assert b"0x00" not in b
                f.write(struct.pack("{0}s0H".format(len(b) + 1), b))
            if parent_loc > 0:
                # Fix up the parent
                end = f.tell()
                f.seek(parent_loc)
                f.write(UINT32.pack(loc))
                nonlocal max_distance
                if loc - parent_loc > max_distance:
                    max_distance = loc - parent_loc
                f.seek(end)

        write_node(trie.root, 0)
        while todo:
            # Using a deque and popleft here causes child nodes
            # to be written consecutively in the output file
            write_node(*todo.popleft())

        print(
            "Written {0:,} nodes, thereof {1:,} single-char nodes and {2:,} multi-char."
            .format(node_cnt, single_char_node_count, multi_char_node_count)
        )
        print("Childless nodes are {0:,}.".format(no_child_node_count))
        print("Maximum fixup distance is {0:,} bytes.".format(max_distance))

    def write_unigram_pointers(self, f):
        """ Unigram sequence: we write pointers to the next level
            for every unigram id. Some ids may not have an associated
            next level, in which case their range is zero. """
        level = self.level0
        # Initialize the pointer list, which always starts with a 0
        # for the 0-th entry
        ptrs = [0]
        # Zero the running bigram pointer index
        ix = 0
        # Loop over all unigram ids
        for i in range(len(self.trie)):
            # Append this id's index to the pointer list
            p = level.d[i]
            # Add the number of bigram entries to our running index
            delta = 0 if p.d is None else len(p.d)
            ix += delta
            ptrs.append(ix)
        # Now, compress the pointer list using Elias-Fano encoding
        ml = MonotonicList()
        print("Uni-pointers are {0:,}".format(len(ptrs)))
        ml.compress(ptrs)
        # ...and write it to our compressed buffer
        f.write(ml.to_bytes())
        print("Uni-pointers: {0}\n".format(ml))

    def write_unigram_frequencies(self, f):
        """ Write the unigram frequency data """
        print("Uni-frequencies are {0:,}".format(len(self.trie)))
        freqs = self.freqs[1]
        d = self.level0.d
        pos = f.tell()
        self.write_frequencies(
            f, [freqs[d[i].cnt] for i in range(len(self.trie))]
        )
        print("Uni-frequencies occupy {0:,} bytes.".format(f.tell() - pos))

    def write_bigram_and_trigram_levels(self, f):
        """ Write the bigram and trigram levels to the binary file """
        level0 = self.level0
        bi_freqs = self.freqs[2]
        tri_freqs = self.freqs[3]
        # Zero the pointer list (only at the bigram level)
        ptrs = []
        # Zero the running bigram->trigram pointer index
        ix = 0
        # Zero the id lists
        bi_ids = []
        tri_ids = []
        # Zero the running prefix sum for the ids
        bi_prefix_sum = 0
        tri_prefix_sum = 0
        # Zero the frequency lists
        bi_fqs = []
        tri_fqs = []

        # Keep a cache of sorted children of top-level
        # unigrams, i.e. a list of (w0, w1) sorted by w1
        # for each w0
        sorted_child_cache = dict()

        def sorted_child_ids(w0):
            try:
                return sorted_child_cache[w0]
            except KeyError:
                s = sorted(level0.d[w0].d.keys())
                sorted_child_cache[w0] = s
                return s

        # Loop over all unigram ids
        for w0 in range(len(self.trie)):
            p = level0.d[w0]
            if p is not None and p.d:
                # Sort the bigrams that start with the unigram w0
                bids = sorted_child_ids(w0)
                # ...and append them to our id list
                for w1 in bids:
                    bi_ids.append(w1 + bi_prefix_sum)
                    pp = p.d[w1]
                    # Also, append to the pointer list into the trigrams level
                    ptrs.append(ix)
                    # Finally, append to the frequency list
                    bi_fqs.append(bi_freqs[pp.cnt])
                    # Now, for the trigram part
                    if pp.d:
                        # Add to the bigram pointer
                        ix += len(pp.d)
                        # Obtain a sorted list of the child ids of the bigram node
                        trids = sorted(pp.d.keys())
                        # At this point we apply a key trick from the
                        # Pibiri & Venturini paper cited in the header:
                        # instead of storing the unigram id of the w2 (third)
                        # word in each trigram (w0, w1, w2), we store
                        # w2's position within the list of children of
                        # w1 in the (ordered) set of (w1, x) bigrams.
                        # This is a much lower number than w2's unigram id,
                        # and through this remapping the Elias-Fano integer
                        # list shrinks considerably - at the cost of a
                        # little more work during lookup.
                        # Retrieve a cached sorted list of children of w1
                        w1_children = sorted_child_ids(w1)
                        remapped_id = 0
                        for w2 in trids:
                            # Find the index of w2 within the list of children
                            # of w1, by bisection
                            remapped_id = bisect_left(w1_children, w2)
                            # If we were not using the remapping technique,
                            # we would simply do this:
                            # remapped_id = w2
                            tri_ids.append(remapped_id + tri_prefix_sum)
                            tri_fqs.append(tri_freqs[pp.d[w2].cnt])
                        tri_prefix_sum += remapped_id
                bi_prefix_sum += bids[-1]
        ptrs.append(ix)

        # Prepare the compressors
        ml = MonotonicList()
        pl = PartitionedMonotonicList()

        # Write the bigram level data
        print("\nBi-ids are {0:,}".format(len(bi_ids)))
        pl.compress(bi_ids)
        # ml.compress(bi_ids)
        f.write(pl.to_bytes())
        print("Bi_ids compressed with partitions: {0:,} bytes".format(len(pl.to_bytes())))
        # print("Bi-ids: {0}\n".format(ml))

        print("Bi-pointers are {0:,}".format(len(ptrs)))
        ml.compress(ptrs)
        bi_ptr_loc = f.tell()
        f.write(ml.to_bytes())
        print("Bi-pointers: {0}\n".format(ml))

        # Write the trigram ids
        # (There are no pointers at the trigram level)
        print("Tri-ids are {0:,}".format(len(tri_ids)))
        pl.compress(tri_ids)
        # ml.compress(tri_ids)
        tri_id_loc = f.tell()
        f.write(pl.to_bytes())
        print("Tri_ids compressed with partitions: {0:,} bytes".format(len(pl.to_bytes())))
        # print("Tri-ids: {0}\n".format(ml))

        pl = ml = None

        print("\nBi-frequencies are {0:,}".format(len(bi_fqs)))
        bi_fq_loc = f.tell()
        self.write_frequencies(f, bi_fqs)
        print("Bi-frequencies occupy {0:,} bytes.".format(f.tell() - bi_fq_loc))
        print("\nTri-frequencies are {0:,}".format(len(tri_fqs)))
        tri_fq_loc = f.tell()
        self.write_frequencies(f, tri_fqs)
        print("Tri-frequencies occupy {0:,} bytes.".format(f.tell() - tri_fq_loc))
        return bi_fq_loc, tri_fq_loc, bi_ptr_loc, tri_id_loc

    def write_frequencies(self, f, freq_ranks):
        """ Write an array containing frequency ranks in a minimal number of bits """
        # Create a dictionary of code words for each frequency rank,
        # using the fewest bits for the most frequent ranks
        codebook = dict()
        cnt = defaultdict(int)
        # Count the frequency ranks
        for fqr in freq_ranks:
            cnt[fqr] += 1
        # Sort the frequency ranks in descending order by how common they are
        sorted_freq_ranks = sorted(cnt.items(), key=lambda e:e[1], reverse=True)
        # Allocate code words to ranks in descending order of frequency
        for ix, (rank, _) in enumerate(sorted_freq_ranks):
            # Number of bits for this code word
            log2 = int(math.log(ix + 2, 2))
            # Allocate the code word to rank i
            # The following expression allocates the code
            # words in the minimal sequence
            # 0, 1, 00, 01, 10, 11, 000, 001, ...
            codebook[rank] = (ix + 2 - (1 << log2), log2)
        # Pack the code words into a bit array
        cwbits = BitArray()
        # Pack the start bits into another bit array
        startbits = BitArray()
        # Note the start bit position every FREQ_QUANTUM_SIZE items
        freq_index = []
        for ix, fqr in enumerate(freq_ranks):
            if ix % self.FREQ_QUANTUM_SIZE == 0 and ix > 0:
                freq_index.append(startbits.num_bits())
            # Retrieve code word and number of bits from code book
            cw, bits = codebook[fqr]
            # Append the code word
            cwbits.append(cw, bits)
            # Append to the start bit sequence, where there is a
            # 1-bit at the start (low bit) of each code word
            startbits.append(1, bits)
        # Make sure that there is a final bit in startbits
        startbits.append(1, 1)
        # Ensure that cwbits has the same size
        cwbits.append(0, 1)
        # Store the codebook-to-freq-rank map
        assert len(sorted_freq_ranks) < 1 << 16
        f.write(UINT16.pack(len(sorted_freq_ranks)))
        for rank, _ in sorted_freq_ranks:
            assert rank < 1 << 16
            f.write(UINT16.pack(rank))
        # Store the frequency index positions
        f.write(UINT32.pack(len(freq_index)))
        for bit_pos in freq_index:
            assert bit_pos < 1 << 32
            f.write(UINT32.pack(bit_pos))
        # Store the number of bytes in cwbits (startbits has same size)
        assert len(cwbits) < 1 << 32
        assert len(cwbits) == len(startbits)
        f.write(UINT32.pack(len(cwbits)))
        f.write(cwbits.to_bytes())
        f.write(startbits.to_bytes())

    # Note that the trie offset must be the first header
    _HEADERS = (
        "_trie",
        "_freqs",
        "_unigram_ptrs",
        "_bigrams",
        "_bigram_ptrs",
        "_trigrams",
        "_unigram_freqs",
        "_bigram_freqs",
        "_trigram_freqs",
    )
    _NUM_HEADERS = len(_HEADERS)

    def write_binary(self, fname):
        """ Write a compressed form of the trigram database to a file """
        print("Writing file '{0}'...".format(fname))
        # Create a byte buffer stream
        f = io.BytesIO()

        # Version header
        f.write(self.VERSION)

        # Make a temporary instance to hold header fields,
        # initialized from the _HEADERS tuple. Each header
        # field is a pointer to a major section of the file.
        class Headers:
            pass

        h = Headers()

        for hdr in self._HEADERS:
            # Associate a field of h with a file offset which
            # will be fixed up later
            setattr(h, hdr[1:] + "_offset", f.tell())
            f.write(UINT32.pack(0))

        def write_padded(b, n):
            assert len(b) <= n
            f.write(b + b"\x00" * (n - len(b)))

        def fixup(ptr, loc=None):
            """ Go back and fix up a previous pointer to point at the
                current offset in the stream """
            fix = f.tell() if loc is None else loc
            f.seek(ptr)
            f.write(UINT32.pack(fix))
            if loc is None:
                f.seek(0, io.SEEK_END)
            else:
                f.seek(loc)

        # Write frequencies list
        write_padded(b"[frequencies]", 16)
        fixup(h.freqs_offset)
        pos = f.tell()
        # Compressing this list would save a few kilobytes but make
        # retrieval slower, so it's probably not worth it
        for level in range(self.MAX_N + 1):
            freqs = self.freqs[level]
            assert len(freqs) < 1 << 32
            f.write(UINT32.pack(len(freqs)))
            for k in sorted(freqs.keys()):
                assert k < 1 << 32
                f.write(UINT32.pack(k))
        print("Frequencies take a total of {0:,} bytes.".format(f.tell() - pos))

        # Write unigram vocabulary
        write_padded(b"[vocabulary]", 16)
        fixup(h.trie_offset)
        pos = f.tell()
        self.write_trie(f, self.trie)
        print("Vocabulary trie takes a total of {0:,} bytes.".format(f.tell() - pos))

        # Write the ngram data
        write_padded(b"[ngrams]", 16)
        pos = f.tell()

        # Write the unigram level
        fixup(h.unigram_ptrs_offset)
        self.write_unigram_pointers(f)
        print("Unigram pointers take a total of {0:,} bytes.".format(f.tell() - pos))
        pos = f.tell()
        fixup(h.unigram_freqs_offset)
        self.write_unigram_frequencies(f)
        print("Unigram frequencies take a total of {0:,} bytes.".format(f.tell() - pos))
        pos = f.tell()

        # Write the bigram and trigram levels
        fixup(h.bigrams_offset)
        bi_fq_loc, tri_fq_loc, bi_ptr_loc, tri_id_loc = self.write_bigram_and_trigram_levels(f)
        fixup(h.bigram_freqs_offset, bi_fq_loc)
        fixup(h.trigram_freqs_offset, tri_fq_loc)
        fixup(h.bigram_ptrs_offset, bi_ptr_loc)
        fixup(h.trigrams_offset, tri_id_loc)
        f.seek(0, io.SEEK_END)

        print("Bigram and trigram levels take a total of {0:,} bytes.".format(f.tell() - pos))

        # Write the entire byte buffer stream to the compressed file
        with open(fname, "wb") as stream:
            stream.write(f.getvalue())

    def load(self, fname):
        """ Open a compressed trigram file and map it into memory """
        with open(fname, "rb") as stream:
            self._b = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)

        mb = memoryview(self._b)
        assert mb[0:16] == self.VERSION

        # Create a CFFI buffer object pointing to the memory map
        self._mmap_buffer = ffi.from_buffer(mb)

        # Unpack all header fields, in the order given
        for hdr, val in zip(
            self._HEADERS,
            struct.unpack(
                "<" + "I" * self._NUM_HEADERS,
                mb[16:16 + 4 * self._NUM_HEADERS]
            )
        ):
            # Assign the file sections to attributes
            # of the self object
            setattr(self, hdr, mb[val:])

        # Cache the trie root header
        self._trie_root = UINT32.unpack_from(self._trie, 0)[0]

        # Instantiate the MonotonicList for unigram pointers
        self._unigram_ptrs_ml = MonotonicList(self._unigram_ptrs)

        # Instantiate the partitioned list for bigrams
        self._bigram_ml = PartitionedMonotonicList(self._bigrams)
        # self._bigram_ml = MonotonicList(self._bigrams)

        # Instantiate the MonotonicList for bigram pointers
        self._bigram_ptrs_ml = MonotonicList(self._bigram_ptrs)

        # Instantiate the partitioned list for trigrams
        self._trigram_ml = PartitionedMonotonicList(self._trigrams)
        # self._trigram_ml = MonotonicList(self._trigrams)

        # Load the freqs rank list into memory
        self.freqs = []
        loc = 0
        for level in range(self.MAX_N + 1):
            num = UINT32.unpack_from(self._freqs, loc)[0]
            loc += 4
            fql = []
            for _ in range(num):
                fql.append(UINT32.unpack_from(self._freqs, loc)[0])
                loc += 4
            self.freqs.append(fql)

        # Get the unigram count and store its logarithm
        ucnt = self.freqs[0][0]
        self.log_ucnt = math.log(ucnt + 1)

    def close(self):
        """ Close the memory map and destroy all references to it """
        if self._b is not None:
            for hdr in self._HEADERS:
                setattr(self, hdr, None)
            self._mmap_buffer = None
            self.freqs = None
            self._unigram_ptrs_ml = None
            self._bigram_ml = None
            self._bigram_ptrs_ml = None
            self._trigram_ml = None
            self._b.close()
            self._b = None


if __name__ == "__main__":

    print("Welcome to the Icegrams trigram compressor\n")

    ngrams = NgramStorage()
    add_all_bigrams = TSV_FILENAME in ("3-grams.sorted", "trigrams-subset.tsv")
    ngrams.compress(TSV_FILENAME, BINARY_FILENAME, add_all_bigrams=add_all_bigrams)
    ngrams.close()
