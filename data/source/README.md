# `data/source/` — Assessment Source Pack (inputs)

This directory is the **only** location the ingestion pipeline reads original
source material from. It is currently **empty by design**.

## Expected files

Place the supplied assessment source pack here, unmodified, using the original
filenames:

```text
01_Support_Policy_v3_CURRENT.pdf
02_Support_Policy_v2_DEPRECATED.pdf
03_Cancellation_and_Service_Credit_SOP_v4.pdf
04_Product_Operations_Guide_and_Known_Issues.pdf
05_Northstar_Logistics_Enterprise_Agreement.pdf
06_LumenWorks_Service_Agreement.pdf
ParcelPilot_Assessment_Data.xlsx
```

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

Once the files are in place:

```bash
python scripts/verify_source_pack.py
```

Script to be written in the ingestion phase. It will check presence and
readability, report page/sheet counts, and record a SHA-256 per file so every
downstream artifact can be tied back to an exact input revision.
