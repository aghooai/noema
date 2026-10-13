"""noema.media.extractors (ADR-002 §5, spec-09 §6.3-§6.5): the jailed extractors, run as
`python -I -m noema.media.extractors.entry <kind>` inside the sandbox. The only package allowed to import Pillow,
pillow-heif, pikepdf, pypdfium2, Docling, Magika, python-magic and markdown-it-py (spec-01 §2 rule 7)."""
