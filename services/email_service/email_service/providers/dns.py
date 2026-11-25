"""Sending-domain DNS checks (DKIM CNAMEs, SPF include, DMARC policy) with dnspython.

The fake verifier is deterministic (development/test only): a domain whose first label starts with
`unverified` fails every check, `nodmarc` fails DMARC only, anything else passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from nexus_core.config import ServiceSettings
from nexus_core.providers import select_adapter

DKIM_TARGET_SUFFIX = "dkim.amazonses.com"
SPF_INCLUDE = "include:amazonses.com"


@dataclass(frozen=True, slots=True)
class DnsResult:
    dkim: str  # pass | fail
    spf: str
    dmarc: str

    @property
    def verified(self) -> bool:
        # Easy DKIM is what SES requires to send; SPF and DMARC are reported as configuration guidance.
        return self.dkim == "pass"


class DnsVerifier(Protocol):
    async def check(self, domain: str, dkim_tokens: list[str]) -> DnsResult: ...


def _status(ok: bool) -> str:
    return "pass" if ok else "fail"


class RealDnsVerifier:
    def __init__(self, *, timeout_seconds: float = 4.0, resolver: Any = None) -> None:
        if resolver is None:
            import dns.asyncresolver

            resolver = dns.asyncresolver.Resolver()
            resolver.lifetime = timeout_seconds
            resolver.timeout = timeout_seconds
        self._resolver = resolver

    async def _records(self, name: str, rdtype: str) -> list[str]:
        import dns.exception
        import dns.resolver

        try:
            answer = await self._resolver.resolve(name, rdtype)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
            return []
        out: list[str] = []
        for record in answer:
            if rdtype == "TXT":
                out.append(b"".join(record.strings).decode(errors="replace"))
            else:
                out.append(str(record.target).rstrip(".").lower())
        return out

    async def check(self, domain: str, dkim_tokens: list[str]) -> DnsResult:
        dkim_ok = bool(dkim_tokens)
        for token in dkim_tokens:
            targets = await self._records(f"{token}._domainkey.{domain}", "CNAME")
            dkim_ok = dkim_ok and f"{token}.{DKIM_TARGET_SUFFIX}" in targets
        spf = await self._records(domain, "TXT")
        spf_ok = any(r.lower().startswith("v=spf1") and SPF_INCLUDE in r.lower() for r in spf)
        dmarc = await self._records(f"_dmarc.{domain}", "TXT")
        dmarc_ok = any(r.replace(" ", "").upper().startswith("V=DMARC1") for r in dmarc)
        return DnsResult(_status(dkim_ok), _status(spf_ok), _status(dmarc_ok))


class FakeDnsVerifier:
    """DEVELOPMENT/TEST ONLY. Deterministic results, no network."""

    async def check(self, domain: str, dkim_tokens: list[str]) -> DnsResult:
        first = domain.split(".", 1)[0]
        if first.startswith("unverified"):
            return DnsResult("fail", "fail", "fail")
        return DnsResult("pass", "pass", "fail" if first.startswith("nodmarc") else "pass")


def build_dns_verifier(settings: ServiceSettings, *, timeout_seconds: float) -> DnsVerifier:
    return select_adapter(
        settings,
        real=lambda: RealDnsVerifier(timeout_seconds=timeout_seconds),
        fake=FakeDnsVerifier,
        name="dns",
    )


def dns_records(domain: str, dkim_tokens: list[str]) -> list[dict[str, str]]:
    """Records the tenant must publish (shown when a domain is added)."""
    records = [
        {"type": "CNAME", "name": f"{t}._domainkey.{domain}", "value": f"{t}.{DKIM_TARGET_SUFFIX}", "purpose": "dkim"}
        for t in dkim_tokens
    ]
    records.append({"type": "TXT", "name": domain, "value": f"v=spf1 {SPF_INCLUDE} ~all", "purpose": "spf"})
    records.append(
        {
            "type": "TXT",
            "name": f"_dmarc.{domain}",
            "value": f"v=DMARC1; p=quarantine; rua=mailto:dmarc@{domain}",
            "purpose": "dmarc",
        }
    )
    return records
