"""credrot: an offline credential-lifecycle manager for local tasks.

The package manages credential *metadata* only. It never stores or derives a
real secret, and every network call is restricted to a loopback verification
stub.
"""

__version__ = "0.1.0"
