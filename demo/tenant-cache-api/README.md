# Tenant Cache API

This deliberately small service is the bundled TraceForge demonstration project.

The profile cache currently keys entries only by `profile_id`. Two tenants can therefore read
each other's cached profile when they use the same identifier. Fix the isolation bug while
preserving TTL and cache-hit behavior, add a regression test, and keep the existing suite green.

Run the checks with:

```bash
python -m pytest -q
```
