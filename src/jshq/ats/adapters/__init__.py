"""ATS adapters: one module per ATS, all normalizing to NormalizedJob.

Uniform signature: async fetch(client, slug, title_filter) -> list[NormalizedJob],
raising AdapterError on any fetch/shape failure (the refresh pipeline catches
per-company). MANUAL is the only ATS_TYPE without an adapter (hand-entered,
not on a board we poll).
"""

from .. import patterns as p
from . import (
    apple,
    ashby,
    atlassian,
    breezy,
    clearcompany,
    greenhouse,
    icims,
    lever,
    oracle_hcm,
    recruitee,
    rippling,
    smartrecruiters,
    workable,
    workday,
)

# Adapters whose fetch() takes a per-run `config` mapping. Only Workday needs
# one: its API demands a searchText, so what it can fetch at all is a
# settings-driven decision rather than a fixed list. Named here so the
# dispatch stays explicit instead of inspecting signatures.
CONFIG_AWARE = {"workday"}

ADAPTERS = {
    p.GREENHOUSE: greenhouse.fetch,
    p.LEVER: lever.fetch,
    p.ASHBY: ashby.fetch,
    p.SMARTRECRUITERS: smartrecruiters.fetch,
    p.WORKDAY: workday.fetch,
    p.ORACLE_HCM: oracle_hcm.fetch,
    p.ICIMS: icims.fetch,
    p.BREEZY: breezy.fetch,
    p.CLEARCOMPANY: clearcompany.fetch,
    p.APPLE: apple.fetch,
    p.ATLASSIAN: atlassian.fetch,
    p.RECRUITEE: recruitee.fetch,
    p.WORKABLE: workable.fetch,
    p.RIPPLING: rippling.fetch,
}
