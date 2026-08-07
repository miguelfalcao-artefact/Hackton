COMPOSE ?= docker compose
KEY     ?= acme-retail-key
BASE    ?= http://localhost:8100

.PHONY: help up down restart rebuild ps logs applog tail psql events reset smoke clean

help:
	@echo "up        build and start the stack"
	@echo "down      stop the stack (data survives)"
	@echo "clean     stop and delete the database volume"
	@echo "rebuild   rebuild the image and restart"
	@echo "logs      follow container stdout"
	@echo "applog    tail the JSON sink from inside the container"
	@echo "tail      tail the JSON sink from the host bind mount"
	@echo "psql      open a psql shell on the database"
	@echo "events    count domain events by type"
	@echo "reset     wipe and reseed the database on next boot"
	@echo "smoke     assert the API is up and behaving"

up:
	$(COMPOSE) up -d --build
	@echo "API on $(BASE)  —  docs at $(BASE)/docs"

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) down -v

restart:
	$(COMPOSE) restart api

rebuild:
	$(COMPOSE) up -d --build api

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f api

applog:
	$(COMPOSE) exec api tail -n 50 -f /var/log/app/api.jsonl

tail:
	@tail -n 50 -f logs/api.jsonl

psql:
	$(COMPOSE) exec postgres psql -U shop -d shop

events:
	$(COMPOSE) exec postgres psql -U shop -d shop -c \
	  "SELECT event_type, count(*) FROM domain_events GROUP BY 1 ORDER BY 2 DESC;"

reset:
	RESET_ON_START=true $(COMPOSE) up -d --force-recreate api
	@echo "database reset; set RESET_ON_START back to false for the next boot"

smoke:
	@echo "-- health"
	@curl -fsS $(BASE)/health && echo
	@echo "-- unauthenticated read must be rejected"
	@test "$$(curl -s -o /dev/null -w '%{http_code}' $(BASE)/v1/products)" = "401" \
	  && echo "401 as expected" || (echo "FAIL: expected 401" && exit 1)
	@echo "-- authenticated read"
	@curl -fsS -H "X-API-Key: $(KEY)" "$(BASE)/v1/products?page_size=3" >/dev/null \
	  && echo "products readable" || (echo "FAIL: products unreadable" && exit 1)
	@echo "-- log sink exists inside the container"
	@$(COMPOSE) exec -T api test -s /var/log/app/api.jsonl \
	  && echo "api.jsonl is being written" || (echo "FAIL: no log sink" && exit 1)
	@echo "smoke OK"
