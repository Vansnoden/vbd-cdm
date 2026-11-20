# utilities.consolidation.get_iso_country_code

### utilities.consolidation.get_iso_country_code(country: [str](https://docs.python.org/3/builtins/stdtypes.html#str)) → [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None)

Get the ISO 3166-1 alpha-2 country code from a name, alias or code.

Resolution order — deterministic, and the earlier a rule is, the less
guessing it does:

1. the folded value is an alpha-2 code already (`'KE'`);
2. it is an alpha-3 code (`'KEN'`);
3. it is a declared alias (`'Ivory Coast'`, `'Swaziland'`);
4. it is a country name, folded (`"Côte d'Ivoire"`, `'kenya'`);
5. it *contains* a country name, and the longest such name wins, with an
   alphabetical tie-break.

Step 5 is the only heuristic, and it is last for a reason: it is what turns
`'Kenya (Siaya)'` into `KE`. Ranking by length first keeps
`'Guinea-Bissau'` from resolving to `GN` through `'Guinea'`, which is
the bug the previous first-match loop had.

* **Parameters:**
  **country** ([*str*](https://docs.python.org/3/builtins/stdtypes.html#str)) – A country name, an alias, or an alpha-2 / alpha-3 code. `None` and
  blank return `None`.
* **Returns:**
  The ISO alpha-2 code, or `None` when nothing matched.
* **Return type:**
  [str](https://docs.python.org/3/builtins/stdtypes.html#str) or None
