# CLAWBENCH Current Progress

Latest scored run per task. Reference timing uses only completed original-agent runs (`pass_at_1`/completion), so failed or partial reference attempts do not define speed targets. `runtime/ref fastest` is current runtime divided by the fastest completed reference runtime; values above 1.0 are slower. `max_main_prompt_tokens` is parsed from main-process `llm.cache_stats prompt=...`; `max_main_shape_chars` is parsed from main-process prompt shape static+dynamic chars when present.

| task | latest run | score | completion | trajectory | behavior | runtime | ref completed runs | ref fastest | runtime/ref fastest | ref best score | ref best score runtime | input tok | output tok | total tok | max main prompt tok | max main shape chars |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| t1-bugfix-discount | 20260611_181109_p22116 | 0.9370 | 1.0000 | 0.7900 | 1.0000 | 210.4s | 5 | 123.9s | 1.70 | 1.0000 | 123.9s | 0 | 0 | 0 | - | - |
| t1-bugfix-discount-perturbed | 20260611_181109 | 0.9920 | 1.0000 | 0.9733 | 1.0000 | 110.5s | 5 | 127.4s | 0.87 | 1.0000 | 127.4s | 0 | 0 | 0 | - | - |
| t1-fs-quick-note | 20260611_181109_p33556 | 0.9600 | 1.0000 | 0.8667 | 1.0000 | 39.3s | 5 | 109.0s | 0.36 | 0.8800 | 109.0s | 0 | 0 | 0 | - | - |
| t1-fs-quick-note-perturbed | 20260611_181109_p656 | 0.9600 | 1.0000 | 0.8667 | 1.0000 | 41.8s | 5 | 115.9s | 0.36 | 0.8800 | 115.9s | 0 | 0 | 0 | - | - |
| t2-add-tests-normalizer | 20260611_181109_p45896 | 0.8176 | 1.0000 | 0.3920 | 1.0000 | 215.5s | 5 | 121.9s | 1.77 | 1.0000 | 121.9s | 0 | 0 | 0 | - | - |
| t2-browser-form-fix | 20260611_181109_p22116 | 0.8050 | 1.0000 | 0.3500 | 1.0000 | 279.8s | 5 | 177.8s | 1.57 | 0.8200 | 198.9s | 0 | 0 | 0 | - | - |
| t2-browser-form-fix-perturbed | 20260611_181109 | 0.9330 | 1.0000 | 0.7767 | 1.0000 | 100.1s | 5 | 206.0s | 0.49 | 0.8200 | 206.0s | 0 | 0 | 0 | - | - |
| t2-config-loader | 20260611_181109_p656 | 0.9471 | 1.0000 | 0.8236 | 1.0000 | 124.7s | 5 | 67.9s | 1.84 | 1.0000 | 123.1s | 0 | 0 | 0 | - | - |
| t2-fs-find-that-thing | 20260611_181109_p33556 | 0.9509 | 1.0000 | 0.8365 | 1.0000 | 613.9s | 5 | 175.3s | 3.50 | 0.8611 | 175.3s | 0 | 0 | 0 | - | - |
| t2-msg-summarize-thread | 20260611_181109_p45896 | 0.9460 | 1.0000 | 0.8200 | 1.0000 | 118.3s | 3 | 155.1s | 0.76 | 0.9190 | 171.4s | 0 | 0 | 0 | - | - |
| t2-priv-redact-doc | 20260611_181109 | 0.9700 | 1.0000 | 0.9000 | 1.0000 | 77.6s | 5 | 125.1s | 0.62 | 1.0000 | 125.1s | 0 | 0 | 0 | - | - |
| t3-data-pipeline-report | 20260611_181109_p656 | 0.9788 | 1.0000 | 0.9294 | 1.0000 | 160.9s | 4 | 155.3s | 1.04 | 1.0000 | 171.1s | 0 | 0 | 0 | - | - |
| t3-data-pipeline-report-perturbed | 20260611_181109_p22116 | 0.9893 | 1.0000 | 0.9644 | 1.0000 | 114.3s | 1 | 185.8s | 0.62 | 0.9333 | 185.8s | 0 | 0 | 0 | - | - |
| t3-data-sql-query | 20260611_181109_p45896 | 0.9400 | 1.0000 | 0.8000 | 1.0000 | 109.7s | 3 | 187.7s | 0.58 | 0.9790 | 223.1s | 0 | 0 | 0 | - | - |
| t3-data-sql-query-perturbed | 20260611_181109_p33556 | 0.9200 | 1.0000 | 0.7333 | 1.0000 | 107.6s | 3 | 235.1s | 0.46 | 0.8590 | 253.0s | 0 | 0 | 0 | - | - |
| t3-feature-export | 20260611_181109_p22116 | 0.9957 | 1.0000 | 0.9857 | 1.0000 | 261.6s | 5 | 153.5s | 1.70 | 0.9333 | 153.9s | 0 | 0 | 0 | - | - |
| t3-feature-export-perturbed | 20260611_181109 | 0.9936 | 1.0000 | 0.9786 | 1.0000 | 177.4s | 5 | 139.5s | 1.27 | 0.9950 | 150.6s | 0 | 0 | 0 | - | - |
| t3-msg-inbox-triage | 20260611_181109_p33556 | 0.9194 | 1.0000 | 0.7314 | 1.0000 | 158.6s | 5 | 148.0s | 1.07 | 0.8973 | 182.6s | 0 | 0 | 0 | - | - |
| t3-msg-inbox-triage-perturbed | 20260611_181109_p656 | 0.9435 | 1.0000 | 0.9783 | 0.7500 | 168.9s | 5 | 150.3s | 1.12 | 0.9164 | 178.0s | 0 | 0 | 0 | - | - |
| t3-web-research-and-cite | 20260611_181109 | 0.9174 | 1.0000 | 0.9467 | 0.6667 | 172.0s | 1 | 268.7s | 0.64 | 0.7815 | 268.7s | 0 | 0 | 0 | - | - |
| t3-web-research-and-cite-perturbed | 20260611_181109_p45896 | 0.9810 | 1.0000 | 0.9368 | 1.0000 | 243.6s | 2 | 206.3s | 1.18 | 0.7871 | 206.3s | 0 | 0 | 0 | - | - |
| t4-browser-research-and-code | 20260611_181109_p22116 | 0.9948 | 1.0000 | 0.9825 | 1.0000 | 190.6s | 4 | 78.1s | 2.44 | 1.0000 | 78.1s | 0 | 0 | 0 | - | - |
| t4-cross-repo-migration | 20260611_181109_p656 | 0.9980 | 1.0000 | 0.9933 | 1.0000 | 115.5s | 5 | 64.2s | 1.80 | 1.0000 | 64.2s | 0 | 0 | 0 | - | - |
| t4-delegation-repair | 20260611_181109_p33556 | 0.9330 | 1.0000 | 0.7767 | 1.0000 | 191.9s | 2 | 248.0s | 0.77 | 0.7517 | 248.0s | 0 | 0 | 0 | - | - |
| t4-life-trip-plan | 20260611_181109_p45896 | 0.9861 | 1.0000 | 0.9538 | 1.0000 | 274.8s | 3 | 240.6s | 1.14 | 0.8690 | 299.1s | 0 | 0 | 0 | - | - |
| t4-memory-recall-continuation | 20260611_181109 | 0.9422 | 1.0000 | 0.8073 | 1.0000 | 157.4s | - | - | - | - | - | 0 | 0 | 0 | - | - |
| t5-hallucination-resistant-evidence | 20260611_181109_p22116 | 0.9685 | 1.0000 | 0.8950 | 1.0000 | 135.7s | 1 | 73.9s | 1.84 | 0.9033 | 73.9s | 0 | 0 | 0 | - | - |
