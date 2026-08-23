# `data/source/` — Assessment Source Pack (inputs)

This directory is the **only** location the ingestion pipeline reads original
source material from. The supplied assessment pack is **present and committed**,
unmodified, under its original filenames.

## The pack

```text
01_Support_Policy_v3_CURRENT.pdf                     CURRENT     effective 1 May 2026
02_Support_Policy_v2_DEPRECATED.pdf                  DEPRECATED  superseded by v3
03_Cancellation_and_Service_Credit_SOP_v4.pdf        CURRENT     effective 15 June 2026
04_Product_Operations_Guide_and_Known_Issues.pdf     CURRENT     updated 14 August 2026
05_Northstar_Logistics_Enterprise_Agreement.pdf      ACTIVE      ACCT-001
06_LumenWorks_Service_Agreement.pdf                  ACTIVE      ACCT-002
ParcelPilot_Assessment_Data.xlsx                     snapshot 2026-08-16 11:00 Asia/Kolkata
```

The status column is not decoration — it is read out of each document's own
preamble at ingestion and is what drives the source-authority precedence chain.
`02_Support_Policy_v2_DEPRECATED.pdf` is deliberately retained: the system has
to be able to demonstrate that it will not answer from it.

## Rules

- **Do not** edit, retype, summarise, or reconstruct these files. Every fact the
  agent states must be traceable to bytes that were actually delivered.
- **Do not** create placeholder or synthetic stand-ins. A missing file must fail
  loudly during ingestion, never be silently substituted.
- Filenames are load-bearing. `_CURRENT` and `_DEPRECATED` are signals that feed
  the source-authority precedence chain — see
  [../../docs/architecture.md](../../docs/architecture.md).
- Everything derived from these files belongs in `data/processed/` or
  `data/index/`, both of which are git-ignored and fully regenerable.

## Verifying the pack

```bash
python scripts/verify_source_pack.py
```

Checks that every expected file is present and readable, reports that no
unexpected file has been added, and records a SHA-256 per file so every
downstream artifact ties back to an exact input revision. The workbook's hash
is also stored in `dataset_metadata` at ingestion, so a database can be traced
to the bytes it was built from.
