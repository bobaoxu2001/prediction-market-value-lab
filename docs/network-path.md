# Network path, and why a strange DNS answer was not a compromise

During a collection outage on 2026-08-25 the Kalshi API hostname resolved to an
address with no plausible relationship to the venue, while unrelated hosts
(`example.com`, `api.weather.gov`) were also failing. That is worth explaining
rather than dismissing, because "the collector fetched from somewhere else" and
"the collector fetched nothing" have very different consequences for an archive.

## What it was

A local proxy client running in **fake-IP TUN mode**.

The signature is unambiguous:

- The host's `198.18.0.0/15` traffic is routed to a `utun` interface.
  `198.18.0.0/15` is RFC 2544 benchmarking space — never routable on the public
  internet, and the conventional pool such clients hand out.
- Querying public resolvers explicitly returned **the same synthetic address**
  as the system resolver. A remote resolver cannot cause that; something local
  is answering every query regardless of the nameserver asked.
- `HTTP_PROXY` / `HTTPS_PROXY` point at a loopback port, so the collector's
  requests egress through that client.

So the odd answer was an allocation from a local proxy's synthetic pool, and the
outage-time address was that client returning an exit-path address while
degraded. **Category: VPN/proxy behaviour. Not resolver poisoning, not a captive
portal, not a logging artefact.**

Confidence is high on the mechanism. It is *not* a forensic reconstruction: the
specific historical answer was observed in a shell, not captured in a query log,
and cannot be replayed. Treat the mechanism as established and that single
observation as explained-but-unreplayable.

## Why the archive is still trustworthy

A proxy in the path does not, by itself, mean the data came from an impostor.
The control is TLS, and it is intact:

| Check | State |
|---|---|
| HTTP client certificate verification | on (library default, never overridden) |
| `verify=False` / `CERT_NONE` / disabled hostname checks | absent from the repository |
| `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS` | unset |
| Trust store | the bundled `certifi` roots |
| Live verification result against the venue | success |

The last two rows carry the argument. The client trusts `certifi`'s roots, which
contain only public CAs — **not** whatever root a MITM proxy installs into the
operating system keychain. A proxy attempting to terminate and re-originate TLS
would therefore fail verification and the request would raise, not silently
return substituted data.

**Could an archived payload have come from an invalid endpoint?** No. Every
response that reached the archive completed a certificate check against the
venue's hostname using public roots. A synthetic *IP* is irrelevant to that: the
name is what is verified, and the proxy tunnels rather than terminates.

## Rules

- **Never weaken TLS verification** to work around a proxy. Disabling
  verification converts this from a routing curiosity into the exact failure the
  verification exists to prevent, and it would do so silently.
- A collection gap is preferable to an unauthenticated fill. The circuit breaker
  opening during the outage was correct behaviour.
- Host-specific proxy and resolver configuration is deliberately not recorded
  here. This repository is public, and the mechanism is what matters.
