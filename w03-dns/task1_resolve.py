#!/usr/bin/env python3
"""Week 3 · Task 1 — Build your own iterative resolver.

Textbook §2.4.2 - §2.4.3.

`dig +trace` walks root -> TLD -> authoritative for you. In this task you do
that walk yourself: start at a root server, read the delegation it returns,
ask the next server, and keep going until somebody answers authoritatively.

You may shell out to `dig` for the transport, or use a DNS library
(`dnspython` is in the container). Either is fine - what matters is that
*you* follow the delegations rather than letting a tool do it.

    python3 task1_resolve.py www.korea.ac.kr
    python3 task1_resolve.py --verify        # check yourself against dig

Pass condition
--------------
`--verify` resolves five names with your resolver and with `dig`, and the
addresses must agree. A name behind a CDN may legitimately return a different
address each time; the harness compares the *set of authoritative nameservers*
you ended at for those, not the address.
"""
import argparse, re, shutil, subprocess, sys

# Root servers. Everything starts here; there is no earlier step.
ROOT_SERVERS = [
    "198.41.0.4",       # a.root-servers.net
    "199.9.14.201",     # b.root-servers.net
    "192.33.4.12",      # c.root-servers.net
]

# (name, kind).  "stable" names must match dig exactly.  "cdn" names are served
# from many replicas and may legitimately give you a different address than dig
# got a second earlier - for those we only require that you reached an answer.
VERIFY_NAMES = [
    ("www.korea.ac.kr", "stable"),
    ("dns.google", "stable"),
    ("en.wikipedia.org", "stable"),
    ("www.stanford.edu", "stable"),
    ("www.microsoft.com", "cdn"),
]


# ------------------------------------------------------------ transport layer
# Everything in this block only moves bytes. Every decision - who to ask next,
# what to do without glue, when to restart on a CNAME - is in Resolver below.
class ResolveError(Exception):
    """The walk could not finish: NXDOMAIN, no usable server, or a loop."""


class BudgetExhausted(ResolveError):
    """Too many questions for one name. Unlike the others this stops EVERYTHING:
    it is never a reason to 'try the next server'."""


class RR:
    """One resource record: owner, ttl, type, rdata (names lower-cased, no dot)."""
    __slots__ = ("owner", "ttl", "type", "rdata")

    def __init__(self, owner, ttl, rtype, rdata):
        self.owner, self.ttl, self.type, self.rdata = owner, ttl, rtype, rdata

    def __repr__(self):
        return f"{self.owner} {self.ttl} {self.type} {self.rdata}"


class Reply:
    """One parsed response: rcode, the aa/ra flag bits, and three sections."""

    def __init__(self, rcode, flags, answer, authority, additional):
        self.rcode = rcode
        self.aa = "aa" in flags          # authoritative answer
        self.ra = "ra" in flags          # server is willing to recurse
        self.answer, self.authority, self.additional = answer, authority, additional


def _norm(name):
    return name.rstrip(".").lower()


def _within(name, zone):
    """Is `name` inside `zone`?  The root zone is the empty string."""
    return zone == "" or name == zone or name.endswith("." + zone)


class DigTransport:
    """Ask ONE server ONE question with recursion switched off, via `dig`.

    Returns a Reply, or None when the server gave no reply at all.
    """
    SECTION = {"ANSWER": "answer", "AUTHORITY": "authority", "ADDITIONAL": "additional"}

    def __init__(self, timeout=2):
        if not shutil.which("dig"):
            raise RuntimeError("dig is not installed (course container: dnsutils)")
        self.timeout = timeout

    def __call__(self, server, qname):
        cmd = ["dig", f"@{server}", qname, "A", "+norecurse", "+noall", "+comments",
               "+answer", "+authority", "+additional",
               f"+time={self.timeout}", "+tries=1"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=self.timeout * 2 + 5).stdout
        except subprocess.TimeoutExpired:
            return None
        return self.parse(out)

    @classmethod
    def parse(cls, text):
        status, flags, cur = None, [], None
        sec = {"answer": [], "authority": [], "additional": []}
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r";; ->>HEADER<<-.*status: (\w+)", line)
            if m:
                status = m.group(1)
                continue
            m = re.match(r";; flags: ([a-z ]*);", line)
            if m:
                flags = m.group(1).split()
                continue
            if line.startswith(";; ") and line.endswith("SECTION:"):
                cur = cls.SECTION.get(line[3:-len("SECTION:")].strip())
                continue
            if not line or line.startswith(";") or cur is None:
                continue
            parts = line.split(None, 4)
            if len(parts) < 5 or parts[2] != "IN":
                continue
            owner, ttl, rtype, rdata = parts[0], parts[1], parts[3], parts[4]
            if rtype in ("NS", "CNAME"):
                rdata = _norm(rdata)
            sec[cur].append(RR(_norm(owner), int(ttl), rtype, rdata))
        if status is None:               # no header at all = timeout / unreachable
            return None
        return Reply(status, flags, **sec)


class Resolver:
    """Your iterative resolver.

    The whole point is that you never ask a server to recurse for you.
    You ask one server, it says "not mine, ask over there", and you go there.

    Suggested shape - but it is yours to design:

        resolve(name) -> (address, path)
            address : the A record you ended up with, as a string
            path    : the servers you asked, in order, so you can show your work

    Things you will hit, in roughly this order:

    1.  A delegation gives you NS *names*, sometimes with glue A records and
        sometimes without. No glue means you have to resolve that nameserver's
        name first - which is another walk. Decide what you do there.
    2.  A server may not answer. Try the next one rather than giving up.
    3.  CNAMEs. The answer you get back may be a different name than the one
        you asked for, and you have to start again with that name.
    4.  Loops. Cap your depth.

    If you shell out to dig, the flag you want is `+norecurse`, so that the
    server you ask replies with a delegation instead of doing the work:

        dig @198.41.0.4 www.korea.ac.kr +norecurse
    """

    MAX_NEST = 8          # R6: nested walks (CNAME restarts + glue-less NS lookups)
    MAX_REFERRALS = 12    # R6: delegations followed inside a single walk
    MAX_QUERIES = 80      # R6: hard budget of questions per resolve() call
    MAX_CNAME = 10        # R6: CNAME hops inside one answer

    def __init__(self, transport=None, roots=None, verbose=False):
        self.transport = transport or DigTransport()
        self.roots = list(roots or ROOT_SERVERS)
        self.verbose = verbose
        self._reset()

    def _reset(self):
        self.path = []               # R1: every server asked, in order (dead ones too)
        self.trace = []              # (nest, why, server, qname, outcome) - for observation.md
        self.glueless_lookups = 0    # R3: how many times a delegation had no glue
        self.queries = 0
        self._stack = []             # names currently being resolved, to catch loops

    def resolve(self, name):
        """Return (address, path). Never asks anyone to recurse for us."""
        self._reset()
        address = self._walk(_norm(name), nest=0, why="main")
        return address, list(self.path)

    # ---------------------------------------------------------------- the walk
    def _walk(self, qname, nest, why):
        if nest > self.MAX_NEST:
            raise ResolveError(f"{qname}: nested more than {self.MAX_NEST} deep")
        if qname in self._stack:
            raise ResolveError(f"{qname}: needs itself to be resolved (loop)")
        self._stack.append(qname)
        try:
            # R2: every walk starts at a root server - there is no earlier step.
            candidates = [("ip", ip) for ip in self.roots]
            zone = ""                    # the zone cut we are currently inside
            for _ in range(self.MAX_REFERRALS):
                kind, reply, server = self._ask_any(candidates, qname, zone, nest, why)
                if kind == "answer":
                    return self._follow_answer(reply, qname, nest)
                if kind in ("nxdomain", "nodata"):
                    raise ResolveError(f"{qname}: {kind.upper()} (authoritative, {server})")
                # kind == "referral": "not mine, ask over there"
                ns_rrs = [rr for rr in reply.authority if rr.type == "NS"]
                zone = ns_rrs[0].owner
                names = []
                for rr in ns_rrs:
                    if rr.rdata not in names:
                        names.append(rr.rdata)
                glue = {}
                for rr in reply.additional:
                    if rr.type == "A" and rr.owner in names:
                        glue.setdefault(rr.owner, []).append(rr.rdata)
                # Servers whose address came with the delegation first; the ones
                # without glue are kept as NAMES and resolved only if we need them.
                candidates = [("ip", ip) for n in names for ip in glue.get(n, [])]
                candidates += [("name", n) for n in names if n not in glue]
            raise ResolveError(f"{qname}: more than {self.MAX_REFERRALS} referrals")
        finally:
            self._stack.pop()

    def _ask_any(self, candidates, qname, zone, nest, why):
        """R4: ask candidates in order until one gives something usable."""
        last = "no server answered"
        for how, item in candidates:
            if how == "name":
                # R3: the delegation named a nameserver but gave no address for it.
                # We have to walk root -> ... -> that name first (a nested resolve).
                self.glueless_lookups += 1
                try:
                    ip = self._walk(item, nest + 1, why=f"glue-less NS {item}")
                except BudgetExhausted:
                    raise
                except ResolveError as e:
                    last = f"NS {item}: {e}"
                    self._note(nest, why, item, qname, "could not resolve this NS name")
                    continue
            else:
                ip = item
            kind, reply = self._ask(ip, qname, zone, nest, why)
            if kind in ("answer", "referral", "nxdomain", "nodata"):
                return kind, reply, ip
            last = f"{ip}: {kind}"
        raise ResolveError(f"{qname}: no usable server ({last})")

    def _ask(self, ip, qname, zone, nest, why):
        if self.queries >= self.MAX_QUERIES:
            raise BudgetExhausted(f"{qname}: query budget of {self.MAX_QUERIES} exhausted")
        self.queries += 1
        self.path.append(ip)
        reply = self.transport(ip, qname)
        kind = self._classify(reply, qname, zone)
        self._note(nest, why, ip, qname, kind)
        return kind, reply

    @staticmethod
    def _classify(reply, qname, zone):
        """What did this server just tell us?

        answer     A/CNAME in the answer section AND the aa bit set
        referral   no answer, NS in authority for a zone strictly closer to qname
        nxdomain / nodata   authoritative "no such name" / "no A record"
        recursive  it answered but is NOT authoritative and offers recursion - we asked
                   for no recursion, so this is a resolver pretending to be the server
        dead       no reply, SERVFAIL/REFUSED, or a lame / no-progress referral
        """
        if reply is None:
            return "dead"
        if reply.rcode not in ("NOERROR", "NXDOMAIN"):
            return "dead"
        if any(rr.type in ("A", "CNAME") for rr in reply.answer):
            if reply.aa:
                return "answer"
            return "recursive" if reply.ra else "dead"
        if reply.rcode == "NXDOMAIN":
            return "nxdomain" if reply.aa else "dead"
        ns = [rr for rr in reply.authority if rr.type == "NS"]
        if ns:
            cut = ns[0].owner
            if _within(qname, cut) and len(cut) > len(zone):   # must make progress
                return "referral"
            return "dead"
        if reply.aa and any(rr.type == "SOA" for rr in reply.authority):
            return "nodata"
        return "dead"

    def _follow_answer(self, reply, qname, nest):
        cname = {rr.owner: rr.rdata for rr in reply.answer if rr.type == "CNAME"}
        addrs = {}
        for rr in reply.answer:
            if rr.type == "A":
                addrs.setdefault(rr.owner, []).append(rr.rdata)
        cur = qname
        for _ in range(self.MAX_CNAME + 1):
            if cur in addrs:
                return addrs[cur][0]
            if cur not in cname:
                break
            cur = cname[cur]
        else:
            raise ResolveError(f"{qname}: CNAME chain longer than {self.MAX_CNAME} (loop?)")
        if cur == qname:
            raise ResolveError(f"{qname}: authoritative answer held no usable record")
        # R5: the chain ended on a name this server cannot answer. Start over with it.
        return self._walk(cur, nest + 1, why=f"CNAME -> {cur}")

    def _note(self, nest, why, server, qname, outcome):
        self.trace.append((nest, why, server, qname, outcome))
        if self.verbose:
            print(f"  {'  ' * nest}[{why}] {server:<16} {qname}: {outcome}")


# ------------------------------------------------------------------- harness
def dig_answer(name):
    """What the system resolver says, for comparison."""
    out = subprocess.run(["dig", "+short", name, "A"],
                         capture_output=True, text=True).stdout
    return [l for l in out.split() if l and l[0].isdigit()]


def verify():
    r, failures = Resolver(), 0
    for name, kind in VERIFY_NAMES:
        try:
            addr, path = r.resolve(name)
        except NotImplementedError:
            print("Nothing implemented yet - write Resolver.resolve first.")
            return 1
        except Exception as e:
            print(f"  FAIL  {name:<22} your resolver raised {e!r}")
            failures += 1
            continue
        expected = dig_answer(name)
        if addr in expected:
            note = ""
        elif kind == "cdn":
            note = "  <- differs, but this name is CDN-hosted. Explain it."
        else:
            note = "  <- should have matched"
            failures += 1
        print(f"  {'FAIL' if note.endswith('matched') else 'ok  '}  {name:<22} "
              f"you={addr:<16} dig={','.join(expected) or '-'}   "
              f"hops={len(path)}{note}")
    print(f"\n  {len(VERIFY_NAMES) - failures}/{len(VERIFY_NAMES)} ok")
    return 1 if failures else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("name", nargs="?", default="www.korea.ac.kr")
    p.add_argument("--verify", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show every step: who was asked, and what they said")
    a = p.parse_args()

    if a.verify:
        sys.exit(verify())

    r = Resolver(verbose=a.verbose)
    addr, path = r.resolve(a.name)
    for i, server in enumerate(path, 1):
        print(f"  {i}. asked {server}")
    print(f"\n  {a.name} -> {addr}")
    print(f"  {len(path)} servers asked, {r.glueless_lookups} glue-less NS lookups")


if __name__ == "__main__":
    main()
