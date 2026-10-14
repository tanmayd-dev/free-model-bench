# shopfront

A tiny stdlib-only product catalogue + order API.

```
python3 -m shopfront            # runs the tests-friendly entry point
python3 -m pytest tests/ -q     # visible test suite
```

The whole HTTP surface lives in `shopfront/api.py`. It works, it is tested,
and it has been extended by three people over four years. Product wants a
v2 API. See `task.md` for what is being asked of you.
