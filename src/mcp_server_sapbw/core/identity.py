"""Who a stored file belongs to, and the one function that decides where it lands.

**The problem this fixes.** Everything the server keeps on disk - the extract cache, the snapshot
store, a generated documentation tree - was named from the profile alias by mapping every awkward
character to an underscore. Two defects followed, and both were measured rather than theorised:

*Distinct profiles collided.* ``prd/eu``, ``prd_eu``, ``prd.eu``, ``prd eu`` and ``prd:eu`` all
produced ``prd_eu.sqlite``. Two profiles - in a partner install, two different **customers** -
shared one cache file and one snapshot store, silently. Nothing failed; the wrong data was there.

*An alias escaped the directory.* The runtime built its cache path itself, as
``cache_dir / f"{system}.sqlite"``, bypassing the sanitising helper that existed for exactly this
purpose. A profile named ``../../escaped`` wrote customer metadata to
``%LOCALAPPDATA%\\escaped.sqlite`` - outside the cache root, unaudited. The helper was correct and
dead; that is the worst arrangement, because the guarantee reads as present while nothing calls it.

:func:`storage_key` replaces both spellings with one. It keeps a readable prefix, because anyone
auditing what is at rest has to be able to tell whose data a file holds, and appends a digest of the
**exact** identity, because that is what makes distinct identities distinct by construction rather
than by hoping the sanitiser is injective. It cannot contain a separator, so it cannot escape.

**Tenant is what separates two customers.** A consultancy running this against several landscapes
has no reason for the aliases to differ - everybody calls their production system ``prd``. Without a
tenant those two are one identity, and the isolation question has no good answer. With it they are
two, and :meth:`StorageIdentity.label` says which is which in every report.

**Environment is declared, not part of the key.** It is reported so an answer can say *this is
production*, and so comparing two systems can notice they are the same environment. It stays out of
the path deliberately: the alias already identifies the connection, and putting a label in the
filename would silently orphan every stored file the moment somebody corrected the label.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

#: Which environment a profile points at. Declared by the operator; the server never infers it from
#: a host name or an alias, because a guess here is the kind that ends with someone reading
#: production numbers believing they are looking at QA.
Environment = Literal["dev", "test", "qa", "preprod", "prod", "sandbox", "unknown"]

#: How much of the readable prefix to keep. Long enough to identify, short enough to keep a path
#: within limits once a suffix and a directory are added.
_MAX_READABLE = 40

#: Digest length. Eight hex characters is 32 bits: ample when the population is the handful of
#: profiles one install has, and short enough that the readable part still leads.
_DIGEST_LENGTH = 8


def _readable(value: str) -> str:
    """A filesystem-safe fragment. Lossy on purpose - the digest carries the exactness."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in value.strip())
    return cleaned[:_MAX_READABLE].strip("_")


def storage_key(system: str, tenant: str | None = None) -> str:
    """A collision-free, directory-contained file stem for one profile's stored data.

    Two properties, and the second is the one that was missing:

    * **Readable.** ``acme-prd-1f4b8c02`` tells an operator whose file this is without opening it.
    * **Injective.** The digest covers the exact ``(tenant, system)`` pair, so two identities can
      never share a stem no matter how similar their aliases look after sanitising.

    Contains only alphanumerics, ``-`` and ``_``, so the result is a single path component and an
    operator-supplied alias cannot traverse out of the cache root.
    """
    exact = f"{tenant or ''}\u001f{system}"
    digest = hashlib.sha256(exact.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    parts = [_readable(part) for part in ((tenant or ""), system) if part.strip()]
    readable = "-".join(part for part in parts if part) or "profile"
    return f"{readable}-{digest}"


@dataclass(frozen=True)
class StorageIdentity:
    """Who a profile belongs to, for isolation and for reporting.

    Frozen because it is used as the identity of stored data: a mutable one would let the file a
    cache is writing to change under it.
    """

    system: str
    tenant: str | None = None
    environment: Environment = "unknown"

    @property
    def key(self) -> str:
        """The file stem for anything stored for this profile. See :func:`storage_key`."""
        return storage_key(self.system, self.tenant)

    @property
    def label(self) -> str:
        """How to name this profile in a report, unambiguously across tenants.

        ``acme/prd (prod)`` rather than ``prd``, because a reader looking at two answers has to see
        which landscape each came from without cross-referencing a config file.
        """
        scoped = f"{self.tenant}/{self.system}" if self.tenant else self.system
        return f"{scoped} ({self.environment})" if self.environment != "unknown" else scoped

    @property
    def is_production(self) -> bool:
        """True when the operator declared this a production system.

        Read rather than inferred. A tool that wants to be careful with production has to be told
        which one it is; guessing from the alias is how ``prd_copy`` gets treated as production and
        ``production_2`` does not.
        """
        return self.environment in ("prod", "preprod")

    @property
    def isolated_by_tenant(self) -> bool:
        """Whether this profile's stored data is separated from another customer's by a tenant.

        False is correct and normal for a single-customer install. It matters only where one machine
        serves several landscapes, which is why it is reported rather than enforced.
        """
        return bool(self.tenant and self.tenant.strip())
