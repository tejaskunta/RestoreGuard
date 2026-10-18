PY := .venv/bin/python

.PHONY: up down seed run demo web test clean

up:
	docker compose up -d --wait
	@echo "postgres ready on 127.0.0.1:5433"

down:
	docker compose down

seed:
	PGPASSWORD=devpass psql -h 127.0.0.1 -p 5433 -U rgadmin -d appdb -f sql/schema.sql

run:
	$(PY) -m restoreguard.verify

demo:
	bash demo.sh

web:
	$(PY) -m restoreguard.web

test:
	$(PY) -m pytest -q

clean:
	docker compose down -v