---
name: restart-app
description: Stop and restart the HVAC website, worker and database, then confirm the login page answers. Use after code or static-file changes, or when the site is down.
disable-model-invocation: true
---

# Restart the HVAC app

Run the restart script from the project root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .claude\skills\restart-app\restart.ps1
```

It stops any running `waitress` and `manage.py worker` processes, starts `run.ps1` in its own hidden window (it starts Postgres if needed, runs migrations, then the worker and the website), and waits up to 120 seconds for http://127.0.0.1:8000/login/ to answer.

- Exit 0 and "App is up": done. Tell the user in one line.
- Exit 1: read the last lines of `worker.err.log` and `.pgdata\server.log`, and report what failed. Do not retry in a loop.

A restart is needed after every code, template or static change, because WhiteNoise caches file stats.
