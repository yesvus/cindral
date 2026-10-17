"""The three answers a run can have, in one place.

Named once because a verdict is written by the queue, read by the panel and
compared by the tests, and three copies of a string that must agree is three
places for them to stop agreeing.
"""

SUCCESS = "success"
FAILURE = "failure"
DEAD = "dead"
PENDING = "pending"
RUNNING = "running"

#: what a run's log is stored at. A device can produce more than this and the
#: tail is what a reader is shown, so the bound is a storage one.
MAX_LOG_CHARS = 64 * 1024
