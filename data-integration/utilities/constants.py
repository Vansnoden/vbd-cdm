# -*- coding: utf-8 -*-
"""Single definitions for values that were duplicated across modules."""

from __future__ import annotations

import re

MAX_IN_VARS = 900

SQLITE_BIND_LIMIT = 30_000

POPULATION_INDICATOR = "SP.POP.TOTL"

ESTIMATE_METHOD = "ESTIMATE"

CASE_STATUS_ESTIMATE = "estimated"

DECISION_AUTO_ACCEPTED = "auto_accepted"

SEASON_DRY = "dry"

SEASON_WET = "wet"

GRID_NAME_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?\s*$")

NUMERIC_TOKEN_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")

GID_LIKE_RE = re.compile(
    r"^(?:GID_\d+_[A-Z]{3}|[A-Z]{3}\.\d+(?:\.\d+)*_\d+)$")

GID_COLUMN_RE = re.compile(r"^GID_(\d+)$", re.IGNORECASE)

NAME_COLUMN_RE = re.compile(r"^NAME_(\d+)$", re.IGNORECASE)

__all__ = [
    "CASE_STATUS_ESTIMATE",
    "DECISION_AUTO_ACCEPTED",
    "ESTIMATE_METHOD",
    "GID_COLUMN_RE",
    "GID_LIKE_RE",
    "GRID_NAME_RE",
    "MAX_IN_VARS",
    "NAME_COLUMN_RE",
    "NUMERIC_TOKEN_RE",
    "POPULATION_INDICATOR",
    "SEASON_DRY",
    "SEASON_WET",
    "SQLITE_BIND_LIMIT",
]