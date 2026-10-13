#!/usr/bin/env python3
"""Week 4 · Task 1 — Build reliable delivery on top of an unreliable channel.

Textbook §3.4 (reliable data transfer) and §3.5 (TCP's sequence numbers).

`UnreliableChannel` below loses packets, reorders them, duplicates them, and
delays them. It is the network as §3.4 models it. Your job is to move a file
across it and have the bytes arrive intact and in order.

That is the whole of TCP's reliability story with the congestion control taken
out, and it is worth building once by hand before you ever trust a socket again.

    python3 task1_rdt.py --verify
"""
import argparse, hashlib, random

PAYLOAD = 8            # bytes per packet - small, so you see the sequencing


class UnreliableChannel:
    """Loses 10%, duplicates 3%, reorders, and delays. Deterministic by seed.

    You may not make it nicer. You may not read its internals. It is the only
    way your sender can reach your receiver.
    """

    def __init__(self, seed=246, loss=0.10, dup=0.03, reorder=0.10):
        self.rng = random.Random(seed)
        self.loss, self.dup, self.reorder = loss, dup, reorder
        self.wire = []          # packets in flight, in no particular order
        self.stats = {"sent": 0, "lost": 0, "duplicated": 0, "delivered": 0}

    def send(self, packet):
        """Hand a packet to the network. It may never come out."""
        self.stats["sent"] += 1
        if self.rng.random() < self.loss:
            self.stats["lost"] += 1
            return
        copies = 2 if self.rng.random() < self.dup else 1
        self.stats["duplicated"] += copies - 1
        for _ in range(copies):
            if self.rng.random() < self.reorder and self.wire:
                self.wire.insert(self.rng.randrange(len(self.wire)), packet)
            else:
                self.wire.append(packet)

    def receive(self):
        """Take the next packet out, or None if the network has nothing."""
        if not self.wire:
            return None
        self.stats["delivered"] += 1
        return self.wire.pop(0)


class Sender:
    """Selective Repeat sender (§3.4.4).

    Packets on the wire:   ("DATA", seq, payload)      sender  -> receiver
                           ("ACK",  seq)               receiver -> sender

    `seq` is the segment index, not a byte offset (PAYLOAD is fixed, so
    byte offset = seq * PAYLOAD; TCP counts bytes, we count segments).

    - Only segments inside [base, base + WINDOW) may be in flight.
    - Each in-flight segment has its own timer, counted in steps. A segment
      whose timer runs out is resent alone - not the whole window (that would
      be Go-Back-N).
    - An ACK is believed only after checking its number: it must name a
      segment we actually sent and have not already seen acknowledged.
      A duplicate or stale ACK changes nothing.
    """

    WINDOW = 8      # segments allowed in flight
    TIMEOUT = 4     # steps before an unacknowledged segment is resent

    def __init__(self, data_channel, ack_channel, data):
        self.out, self.inp = data_channel, ack_channel
        self.segments = [data[i:i + PAYLOAD] for i in range(0, len(data), PAYLOAD)]
        self.n = len(self.segments)
        self.base = 0               # lowest segment not yet acknowledged
        self.next = 0               # next segment never sent
        self.acked = set()
        self.sent_at = {}           # seq -> step of its latest transmission
        self.now = 0
        self.stats = {"first_tx": 0, "retx": 0, "dup_acks": 0, "bogus_acks": 0}

    def step(self):
        """Do one unit of work. Return False when you believe you are done."""
        self.now += 1

        # 1. Read every ACK that has arrived, and check its number before use.
        while (pkt := self.inp.receive()) is not None:
            if not (isinstance(pkt, tuple) and len(pkt) == 2 and pkt[0] == "ACK"):
                self.stats["bogus_acks"] += 1
                continue
            seq = pkt[1]
            if not (0 <= seq < self.next):          # never sent: ignore
                self.stats["bogus_acks"] += 1
            elif seq in self.acked:                 # duplicate ACK: ignore
                self.stats["dup_acks"] += 1
            else:
                self.acked.add(seq)
                self.sent_at.pop(seq, None)
        while self.base in self.acked:              # slide the window
            self.base += 1

        if self.base >= self.n:
            return False                            # everything acknowledged

        # 2. Resend each in-flight segment whose own timer has expired.
        for seq, t in list(self.sent_at.items()):
            if self.now - t >= self.TIMEOUT:
                self._send(seq)
                self.stats["retx"] += 1

        # 3. Send new segments while the window has room.
        while self.next < self.n and self.next < self.base + self.WINDOW:
            self._send(self.next)
            self.stats["first_tx"] += 1
            self.next += 1
        return True

    def _send(self, seq):
        self.out.send(("DATA", seq, self.segments[seq]))
        self.sent_at[seq] = self.now


class Receiver:
    """Selective Repeat receiver.

    - Every DATA packet is acknowledged by its own number, including ones we
      already have: if a duplicate shows up, our earlier ACK was probably
      lost, and staying silent would make the sender resend forever.
    - Out-of-order segments are buffered; only the contiguous prefix counts
      as delivered. A duplicate overwrites nothing - the first copy is kept.
    """

    def __init__(self, data_channel, ack_channel):
        self.inp, self.out = data_channel, ack_channel
        self.buffer = {}            # seq -> payload, received but maybe not in order
        self.expected = 0           # first seq not yet delivered in order
        self.delivered = bytearray()
        self.stats = {"dup_data": 0, "out_of_order": 0}

    def step(self):
        while (pkt := self.inp.receive()) is not None:
            if not (isinstance(pkt, tuple) and len(pkt) == 3 and pkt[0] == "DATA"):
                continue
            _, seq, payload = pkt
            self.out.send(("ACK", seq))              # always (re)acknowledge
            if seq < self.expected or seq in self.buffer:
                self.stats["dup_data"] += 1          # already have it: drop
                continue
            if seq != self.expected:
                self.stats["out_of_order"] += 1
            self.buffer[seq] = payload
            while self.expected in self.buffer:      # deliver in order
                self.delivered += self.buffer.pop(self.expected)
                self.expected += 1

    def data(self):
        """The bytes reassembled so far (in-order prefix only)."""
        return bytes(self.delivered)


# ------------------------------------------------------------------- harness
def verify(seed=246, size=2000, max_steps=200_000):
    original = bytes(random.Random(seed).getrandbits(8) for _ in range(size))
    up, down = UnreliableChannel(seed), UnreliableChannel(seed + 1)

    # Data goes out over `up`, ACKs come back over `down`. Both are unreliable.
    sender = Sender(up, down, original)
    receiver = Receiver(up, down)

    for _ in range(max_steps):
        alive = sender.step()
        receiver.step()
        if not alive and len(receiver.data() or b"") >= size:
            break

    got = receiver.data() or b""
    ok = hashlib.sha256(got).hexdigest() == hashlib.sha256(original).hexdigest()
    print(f"  bytes    sent {size}   received {len(got)}")
    print(f"  channel  {up.stats}")
    print(f"  result   {'IDENTICAL' if ok else 'CORRUPTED OR INCOMPLETE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--verify", action="store_true")
    p.add_argument("--seed", type=int, default=246)
    a = p.parse_args()
    raise SystemExit(verify(a.seed) if a.verify else p.print_help())
