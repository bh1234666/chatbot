# Stress Tools

Disposable harnesses for long-running local validation.

- `run_environment_maintenance.py`: drives `/v1/environment/*` against isolated
  temporary projects and records SSE workflow/monitor events.
- `run_parallel_stress.py`: starts the chatbot backend, runs group chat and
  environment maintenance in parallel, then writes an aggregate report.

Outputs go under `stress_tools/runs/` and are not required by production code.
