.PHONY: help install dev-install test prompt-audit prompt-audit-full prompt-audit-watch lint fmt typecheck run clean

help:
	@echo "install      安装运行时依赖"
	@echo "dev-install  安装开发依赖(含 pytest/ruff/mypy)"
	@echo "test         运行测试(离线,纯逻辑模块)"
	@echo "prompt-audit       提示词漂移审计(快速)"
	@echo "prompt-audit-full  提示词漂移审计(完整)"
	@echo "prompt-audit-watch 定期重复提示词漂移审计"
	@echo "lint         ruff 检查"
	@echo "fmt          ruff 自动格式化"
	@echo "typecheck    mypy 静态类型检查"
	@echo "run          本地启动服务(单 worker —— 进程内状态要求!)"

install:
	pip install -r requirements.txt

dev-install:
	pip install -r requirements-dev.txt

test:
	pytest

prompt-audit:
	python stress_tools/audit_prompt_hygiene.py --show-plan -q

prompt-audit-full:
	python stress_tools/audit_prompt_hygiene.py --full --show-plan -q --write-report logs/prompt_audit_latest.md

prompt-audit-watch:
	python stress_tools/audit_prompt_hygiene.py --full --repeat-minutes 30 -q --report-dir logs/prompt_audits

lint:
	ruff check app tests

fmt:
	ruff format app tests

typecheck:
	mypy app

# 重要:全工程跨请求状态(幂等缓存/ledger/锁/metrics)都在进程内存,
# 必须单 worker 运行,否则会出现难复现的数据问题。
run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
