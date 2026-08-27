# Duration parser repair task

`normalize_seconds` accepts non-negative integer durations and rejects all other input types.

The public behavior is almost correct, but Python's `bool` subtype relationship means `True` and
`False` are currently accepted as seconds. Fix the implementation without changing the tests or
the function signature.

Run the full test suite with:

```bash
python -m pytest -q
```
