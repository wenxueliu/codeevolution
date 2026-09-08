.PHONY: build start stop restart status test

build:
	.venv/bin/python scripts/service.py build

start:
	.venv/bin/python scripts/service.py start

stop:
	.venv/bin/python scripts/service.py stop

restart:
	.venv/bin/python scripts/service.py restart

status:
	.venv/bin/python scripts/service.py status

test:
	.venv/bin/python -m pytest -q
	cd web && npm run test:coverage
