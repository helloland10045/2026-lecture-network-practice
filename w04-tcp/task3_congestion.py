#!/usr/bin/env python3
"""Week 4 · Task 3 — Beat the fixed window.

Textbook §3.7.

`FixedWindow` is a sender that never adapts. It picks a window and keeps it,
forever, no matter what the network says back. It is not a strawman: it is what
you get if you skip congestion control entirely, and it was the internet's
actual failure mode in October 1986.

Write `YourControl` and beat it on the harness:

    python3 bench.py
    python3 bench.py --yours

The interface is two events and one number:

    .window        how many packets you are willing to have in flight
    .on_ack()      one packet made it there and back
    .on_loss()     a packet was dropped, or timed out waiting for its ACK

That is all the information a real TCP sender has. It cannot see the queue,
it cannot see the link rate, and neither can you. You infer them from these
two events, which is the entire idea of §3.7.
"""


class FixedWindow:
    """Send 64 packets at a time and never listen."""

    def __init__(self):
        self.window = 64

    def on_ack(self):
        pass

    def on_loss(self):
        pass


class YourControl:
    """Your congestion control.

    Things worth knowing before you start:

    * The link drains one packet per slot and the round trip is 20 slots, so
      the pipe holds about 20 packets. Above that you are only filling a queue.
    * The queue is 10 packets deep and drops from the tail. Filling it does not
      make you faster - it makes you slower, and everybody behind you too.
    * Cutting hard on every loss costs you throughput. Not cutting costs you
      correctness. §3.7 is the argument about where between those to sit.
    * You are allowed to grow differently before and after your first loss.
      That distinction has a name in the textbook.

    Design (all inferred from ACK / loss events; no peeking at the link):

    1. Slow start: +1 per ACK (window doubles every round trip) until the
       first loss.
    2. Measure, don't guess: during a loss episode every lost packet shrinks
       the window by one and is not replaced (packet conservation). When a
       full window of ACKs arrives with no new loss, the window that is left
       is the number of packets the network actually held = pipe + queue
       (here it settles at 29-30 = 20 in the pipe + 10 in the queue).
    3. Converge to the pipe, not to the drop point: cut that measured value
       to BETA of it. 0.6 x ~30 = ~18, just under the ~20-packet pipe, so
       the queue drains instead of staying full.
    4. Probe gently: +AI packets per round trip (AI/window per ACK). Slow
       probing means the next loss episode comes rarely.
    5. A timeout fires 3 round trips after the send, so a loss reported in
       the 3 windows of ACKs after a cut belongs to the window before the
       cut. Those are ignored instead of cutting a second time.
    """

    BETA = 0.6          # fraction of measured (pipe + queue) to keep
    AI = 0.25           # packets added per round trip after the first loss
    QUIET_RTTS = 3      # = bench TIMEOUT / RTT

    def __init__(self):
        self.window = 1.0
        self.slow_start = True
        self.recovering = False     # inside a loss episode, deflating
        self.clean_acks = 0         # ACKs since the last loss in this episode
        self.quiet = 0              # ACKs left during which losses are stale

    def on_ack(self):
        if self.quiet:
            self.quiet -= 1
        if self.recovering:
            self.clean_acks += 1
            if self.clean_acks >= self.window:      # a full clean window
                self.recovering = False
                self.window = max(2.0, self.window * self.BETA)
                self.quiet = int(self.QUIET_RTTS * self.window)
            return                                   # no growth while measuring
        if self.slow_start:
            self.window += 1
        else:
            self.window += self.AI / self.window

    def on_loss(self):
        if self.quiet:
            return                                   # stale: sent before the cut
        self.slow_start = False
        self.recovering = True
        self.clean_acks = 0
        self.window = max(1.0, self.window - 1)      # do not replace the lost one
