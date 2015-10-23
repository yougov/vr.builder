import sys
import collections

PY32 = sys.version_info >= (3, 2)
DefragResult = collections.namedtuple('DefragResult', 'url fragment')
_defrag = (lambda x: x) if PY32 else lambda x: DefragResult(*x)
"""
Return a Python 3.2 compatible result from urldefrag.
TODO: replace with python-futures invocation.
"""
