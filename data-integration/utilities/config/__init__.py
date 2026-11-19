# -*- coding: utf-8 -*-
"""
Package marker for ``utilities.config``.

Deliberately empty of code (finding §3.3): batch 3's ``New FILE
utilities/config/source_spec.py`` declared that this package marker must exist
and nothing created it, so ``main.py`` and
``etl/transformers/mapping_transformer.py`` — both of which import
``utilities.config.source_spec`` at module scope — depended on PEP 420 namespace
resolution that no batch declared as intentional.

Nothing is imported here. ``utilities.config.source_spec`` pulls ``pydantic`` and
``yaml``; re-exporting it from this ``__init__`` would mean that importing
anything else under ``utilities.config`` in future pays that cost, and would give
two import paths (``utilities.config.SourceSpec`` and
``utilities.config.source_spec.SourceSpec``) for one class, which is how a
``type`` check starts failing for reasons nobody can see.
"""