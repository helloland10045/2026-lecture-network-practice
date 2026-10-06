#!/usr/bin/env python3
"""Week 3 · Task 2 — Does DNS actually steer you? Measure it.

Textbook §2.4.3 (records) and §2.5 (CDNs).

The lecture claims two things:

    (a) most large sites are served by a CDN, reached through a CNAME chain
    (b) DNS steers each user to a *nearby* replica

Both are testable from your laptop, and one of them is harder to prove than
the slide makes it look. Your job is to produce the evidence and a number.

    python3 task2_steering.py --collect        # gather the raw data
    python3 task2_steering.py --report         # your analysis

What you have to build
----------------------
1.  For each hostname in SITES, follow the CNAME chain to its end and record
    every hop. `--collect` should leave the raw data in out/chains.json.

2.  Decide, for each site, whether it is served by a **third party**.
    This is the hard part and there is no single right answer:

      - `www.microsoft.com` ends at `akamaiedge.net`     - clearly third party
      - `www.netflix.com`   stops inside `netflix.com`   - own CDN, not third party
      - some sites have no CNAME at all and still sit behind a CDN (anycast)
      - `foo.cloudfront.net` and `foo.s3.amazonaws.com` are both Amazon,
        but they are not the same service

    Write down the rule you used and **defend it in observation.md**. A rule
    that just compares the last two labels will be wrong on at least one of
    the sites below; find which, and say so.

3.  Ask **two different resolvers** for the same name and compare the
    addresses you get back. If DNS really steers by location, a CDN-hosted
    name should answer differently to resolvers sitting in different places.

        RESOLVERS below has your system resolver and two public ones.

    Report: of N CDN-hosted sites, how many returned a different address set
    from a different resolver? Claim (b) predicts most of them. Check it.

Pass condition
--------------
There is no fixed answer. You pass by producing, in out/report.md:

  - the table: site | chain length | final zone | third party? | your rule's verdict
  - the steering number: "X of N sites answered differently to a different resolver"
  - at least one site where your classification rule was wrong, and why
"""
import argparse, datetime, itertools, json, os, re, subprocess
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")

SITES = [
    "www.microsoft.com",     # Akamai, multi-hop
    "www.netflix.com",       # own CDN
    "www.adobe.com",
    "www.cnn.com",
    "www.apple.com",
    "www.korea.ac.kr",       # no CDN at all
    "www.stanford.edu",
    "www.bbc.co.uk",
    "www.spotify.com",
    "www.github.com",
    "www.wikipedia.org",
    "www.nytimes.com",
]

RESOLVERS = {
    "system": None,          # whatever is in your resolv.conf
    "google": "8.8.8.8",
    "quad9":  "9.9.9.9",
}


def dig(name, rtype="A", server=None):
    """Raw lookup. Transport only - the thinking is yours."""
    # +time/+tries: a blocked resolver should fail in ~3 s, not 15 s per query
    args = ["dig", "+short", "+time=3", "+tries=1", name, rtype]
    if server:
        args.insert(1, f"@{server}")
    out = subprocess.run(args, capture_output=True, text=True).stdout
    # lines starting with ';' are dig's own error messages ("communications error...")
    return [l.strip() for l in out.splitlines() if l.strip() and not l.startswith(";")]


IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _clean(name):
    return name.strip().rstrip(".").lower()


# ----------------------------------------------------------------- collection
def follow_chain(name, limit=10):
    """B1: follow the CNAME chain one hop at a time. Returns [name, hop1, hop2, ...]."""
    chain = [_clean(name)]
    for _ in range(limit):
        hops = [l for l in dig(chain[-1], "CNAME") if not IP_RE.match(l)]
        if not hops:
            break
        nxt = _clean(hops[0])
        if nxt in chain:                       # a loop - stop rather than spin
            break
        chain.append(nxt)
    return chain


def addresses(name, server):
    """The A records one resolver hands back for `name` (the resolver walks the chain)."""
    return sorted(l for l in dig(name, "A", server) if IP_RE.match(l))


# AS number -> who it is. Best effort, only used to look behind names with NO cname.
AS_NAMES = {13335: "Cloudflare", 20940: "Akamai", 16625: "Akamai", 21342: "Akamai",
            54113: "Fastly", 16509: "Amazon", 14618: "Amazon", 15169: "Google",
            8075: "Microsoft", 36459: "GitHub", 2906: "Netflix", 40027: "Netflix",
            14907: "Wikimedia", 714: "Apple", 6185: "Apple"}


def asn_of(ip):
    """Team Cymru answers 'which AS announces this IP' over plain DNS (TXT)."""
    rev = ".".join(reversed(ip.split(".")))
    for line in dig(f"{rev}.origin.asn.cymru.com", "TXT"):
        m = re.match(r'"?(\d+)', line)
        if m:
            return int(m.group(1))
    return None


def _collect_site(site):
    answers = {}
    for rname, server in RESOLVERS.items():
        # asked twice: if one resolver disagrees with ITSELF, that is rotation, not steering
        answers[rname] = {"first": addresses(site, server), "second": addresses(site, server)}
    ips = answers["system"]["first"] or next((v["first"] for v in answers.values() if v["first"]), [])
    asn = asn_of(ips[0]) if ips else None
    return {"chain": follow_chain(site), "answers": answers,
            "asn": {"ip": ips[0] if ips else None, "asn": asn, "name": AS_NAMES.get(asn)}}


def collect(network="default"):
    """B1-B3: raw chains and per-resolver answers -> out/chains.json.

    Run it once per network (`--network campus`, `--network tether`); each run is
    MERGED into the file under its label, so both vantage points are kept.
    """
    path = os.path.join(OUT, "chains.json")
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_collect_site, SITES))
    for site, res in zip(SITES, results):
        res["collected"] = stamp
        data.setdefault(site, {"networks": {}})["networks"][network] = res
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    got = sum(1 for r in results if r["answers"]["system"]["first"])
    print(f"  network '{network}': {len(SITES)} sites, {got} resolved by the system resolver -> {path}")
    for rname in RESOLVERS:
        ok = sum(1 for r in results if r["answers"][rname]["first"])
        print(f"    {rname:<7} answered {ok}/{len(SITES)}")


# --------------------------------------------------------------------- the rule
# Hand-written knowledge. This is a JUDGMENT table - edit it where you disagree.
CDN_ZONES = {                                   # zone suffix -> who runs it
    "akamaiedge.net": "Akamai", "edgekey.net": "Akamai", "akadns.net": "Akamai",
    "edgesuite.net": "Akamai", "akamai.net": "Akamai", "akamaized.net": "Akamai",
    "fastly.net": "Fastly", "fastlylb.net": "Fastly",
    "cloudfront.net": "Amazon CloudFront", "cloudflare.net": "Cloudflare",
    "azureedge.net": "Azure CDN", "azurefd.net": "Azure Front Door",
    "trafficmanager.net": "Azure Traffic Manager", "edgecastcdn.net": "Edgio",
    "amazonaws.com": "Amazon (S3/ELB - hosting, not the CloudFront CDN)",
}
SAME_ORG = [                                    # different domains, one organisation
    {"wikipedia.org", "wikimedia.org"},
    {"bbc.co.uk", "bbc.net.uk", "bbc.com"},
    {"netflix.com", "nflxso.net", "nflxvideo.net", "nflximg.net"},
    {"github.com", "githubusercontent.com", "github.io"},
    {"spotify.com", "scdn.co", "spotifycdn.com"},
]
THIRD_PARTY_AS = {"Cloudflare", "Akamai", "Fastly", "Amazon"}
MULTI_SUFFIX = {"co.uk", "org.uk", "ac.uk", "gov.uk", "ac.kr", "co.kr", "or.kr", "go.kr",
                "ne.kr", "com.au", "co.jp", "co.nz", "com.br", "com.cn", "com.hk", "co.in"}


def last_two(name):
    """The naive key: the last two labels. 'www.bbc.co.uk' -> 'co.uk'."""
    return ".".join(_clean(name).split(".")[-2:])


def registrable(name):
    """Last two labels, except under a known two-part suffix (co.uk, ac.kr, ...)."""
    labels = _clean(name).split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_SUFFIX:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def rule_third_party(site, chain):
    """THE RULE: a site is third-party iff the last hop of its CNAME chain lives under a
    different registrable domain than the site itself. It looks at names only."""
    return registrable(chain[-1]) != registrable(site)


def cdn_owner(name):
    for zone, owner in CDN_ZONES.items():
        if name == zone or name.endswith("." + zone):
            return owner


def judge(site, net):
    """Ground truth as best I can say it: (True/False/None, why).  None = decide by hand."""
    chain, final = net["chain"], net["chain"][-1]
    if cdn_owner(final):
        return True, f"CNAME ends in {cdn_owner(final)}"
    asn_name = (net.get("asn") or {}).get("name")
    if asn_name in THIRD_PARTY_AS:
        how = "no CNAME" if len(chain) == 1 else f"CNAME stays in {registrable(final)}"
        return True, f"{how}, but the address sits in {asn_name}'s network (anycast/edge)"
    if registrable(final) == registrable(site):
        return False, "stays inside its own domain" + (" (no CNAME at all)" if len(chain) == 1 else "")
    if any(registrable(site) in grp and registrable(final) in grp for grp in SAME_ORG):
        return False, f"{registrable(final)} is a different domain, but the same organisation"
    return None, f"{registrable(final)}: unknown owner - decide by hand"


# ----------------------------------------------------------------------- report
def _sets(net):
    return {r: v for r, v in net["answers"].items() if v["first"]}


def steering(site_nets):
    """For one CDN-hosted site: did a different resolver / network see different addresses?"""
    pairs = []                                   # (label, setA, setB, stableA, stableB)
    for nname, net in site_nets.items():
        S = _sets(net)
        for r1, r2 in itertools.combinations(S, 2):
            pairs.append((f"{nname}: {r1} vs {r2}", S[r1], S[r2]))
    for (n1, a), (n2, b) in itertools.combinations(site_nets.items(), 2):
        for r in RESOLVERS:
            if a["answers"][r]["first"] and b["answers"][r]["first"]:
                pairs.append((f"{r}: {n1} vs {n2}", a["answers"][r], b["answers"][r]))
    out = {"differs": [], "disjoint": [], "stable": [], "noisy": False}
    for nname, net in site_nets.items():
        for r, v in _sets(net).items():
            if set(v["first"]) != set(v["second"]):
                out["noisy"] = True              # the SAME resolver disagreed with itself
    for label, x, y in pairs:
        sx, sy = set(x["first"]), set(y["first"])
        if sx != sy:
            out["differs"].append(label)
            if not sx & sy:
                out["disjoint"].append(label)
            if set(x["first"]) == set(x["second"]) and set(y["first"]) == set(y["second"]):
                out["stable"].append(label)
    return out


def report():
    """Read out/chains.json and write out/report.md (table, rule, errors, steering number)."""
    path = os.path.join(OUT, "chains.json")
    if not os.path.exists(path):
        raise SystemExit("no out/chains.json - run `python3 task2_steering.py --collect` first")
    data = json.load(open(path, encoding="utf-8"))
    networks = sorted({n for d in data.values() for n in d["networks"]})
    first = networks[0]

    rows, wrong, cdn_sites = [], [], {}
    for site in SITES:
        nets = data.get(site, {}).get("networks", {})
        if first not in nets or not nets[first]["answers"]["system"]["first"]:
            rows.append((site, "-", "-", "no data", "-", "-"))
            continue
        net = nets[first]
        chain = net["chain"]
        truth, why = judge(site, net)
        rule = rule_third_party(site, chain)
        yn = {True: "yes", False: "no", None: "?"}
        ok = "?" if truth is None else ("yes" if truth == rule else "**NO - wrong**")
        rows.append((site, len(chain) - 1, registrable(chain[-1]) if len(chain) > 1 else registrable(site) + " (itself)",
                     f"{yn[truth]} - {why}", yn[rule], ok))
        if truth is not None and truth != rule:
            wrong.append((site, chain, truth, rule, why))
        if truth:
            cdn_sites[site] = nets
        naive = last_two(chain[-1]) != last_two(site)
        if naive != rule:
            rows[-1] = rows[-1][:5] + (rows[-1][5] + f" (the last-two-labels rule would say {'yes' if naive else 'no'})",)

    N = len(cdn_sites)
    res = {s: steering(n) for s, n in cdn_sites.items()}
    any_d = [s for s in res if res[s]["differs"]]
    disj = [s for s in res if res[s]["disjoint"]]
    stab = [s for s in res if res[s]["stable"]]
    noisy = [s for s in res if res[s]["noisy"]]
    stamp = {n: next(iter(d["networks"][n]["collected"] for d in data.values() if n in d["networks"]), "?")
             for n in networks}

    L = ["# Week 3 · Task 2 report - does DNS steer you?", "",
         f"Networks measured: {', '.join(f'**{n}** ({stamp[n]})' for n in networks)}.  "
         f"Resolvers: " + ", ".join(f"{k} ({v or 'resolv.conf'})" for k, v in RESOLVERS.items()) + ".",
         "The table below is from the first network (" + first + "); `chain length` = number of CNAME hops.", "",
         "## B1 / B4 - chains and the third-party verdict", "",
         "| site | chain length | final zone | third party? (judgment) | your rule's verdict | rule right? |",
         "|---|---|---|---|---|---|"]
    L += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    L += ["", "**The rule:** a site is *third party* iff the last hop of its CNAME chain is under a different "
              "registrable domain (last two labels, or three under co.uk / ac.kr ...) than the site itself. "
              "It reads names only. The *judgment* column is my reading of who actually runs the final zone "
              "(`CDN_ZONES`, `SAME_ORG`, and for names with no CNAME the AS that announces the address).", "",
          "## Where the rule is wrong", ""]
    if wrong:
        for site, chain, truth, rule, why in wrong:
            L.append(f"- **{site}** - chain `{' -> '.join(chain)}`. Rule says "
                     f"{'third party' if rule else 'not third party'}, judgment says "
                     f"{'third party' if truth else 'not third party'}: {why}.")
    else:
        L.append("- Under this judgment table the rule made no mistake on the measured sites. "
                 "That only moves the doubt into the judgment table, which is also a human guess.")
    unknown = [r[0] for r in rows if str(r[3]).startswith("?")]
    if unknown:
        L += ["", f"Not decided by the tables (decide by hand): {', '.join(unknown)}."]
    L += ["", f"## B5 - the steering number ({len(networks)} network(s): {', '.join(networks)})", "",
          f"**{len(any_d)} of {N} CDN-hosted sites answered differently to a different resolver or network** "
          f"(address set not identical).", "",
          f"- {len(disj)} of {N}: some pair of vantage points shared **no** address at all",
          f"- {len(stab)} of {N}: differed even though each side gave the same answer on its own repeat "
          f"(the only differences that cannot be rotation)",
          f"- {len(noisy)} of {N}: the *same* resolver already disagreed with itself between two queries "
          f"seconds apart, so a difference there proves nothing about location", ""]
    if len(networks) < 2:
        L += ["Only one network was measured, so this compares resolvers, not places. "
              "Claim (b) is about where you are: run `--collect --network <other>` from a second network.", ""]
    L += ["| site | differs | disjoint | stable | self-disagrees |", "|---|---|---|---|---|"]
    for s in res:
        r = res[s]
        L.append(f"| {s} | {len(r['differs'])} | {len(r['disjoint'])} | {len(r['stable'])} | "
                 f"{'yes' if r['noisy'] else 'no'} |")
    L += ["", "Addresses seen (first probe):", "", "| site | network | " + " | ".join(RESOLVERS) + " |",
          "|---|---|" + "---|" * len(RESOLVERS)]
    for s in cdn_sites:
        for n, net in cdn_sites[s].items():
            L.append(f"| {s} | {n} | " + " | ".join(", ".join(net["answers"][r]["first"]) or "-" for r in RESOLVERS) + " |")
    L += ["", "## Part A - the capture (fill in from your own Wireshark file)", "",
          "- A3 delegation response: packet number **TODO**   answer response: packet number **TODO**",
          "- A4 largest DNS response: **TODO** bytes - what made it large: **TODO**", ""]
    with open(os.path.join(OUT, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"  wrote out/report.md - {len(any_d)} of {N} CDN-hosted sites differ; "
          f"{len(wrong)} site(s) where the rule was wrong")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--collect", action="store_true")
    p.add_argument("--report", action="store_true")
    p.add_argument("--network", default="default",
                   help="label for this vantage point, e.g. campus or tether")
    a = p.parse_args()
    os.makedirs(OUT, exist_ok=True)
    if a.collect:
        collect(a.network)
    elif a.report:
        report()
    else:
        p.print_help()
