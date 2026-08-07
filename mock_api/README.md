# E-commerce Mock API

Uma plataforma de e-commerce fictícia, funcional e auto-contida, que roda inteira em Docker Compose.
Existe para ser o **alvo observado** do hackathon: ela produz logs e eventos de negócio realistas
sobre os quais a solução do grupo é construída.

O tráfego que exercita esta API vem do repositório irmão **`traffic-generator`**.

---

## Subir

```bash
cp .env.example .env          # opcional: tudo já tem default
docker compose up -d --build
```

- API: <http://localhost:8100>
- Docs OpenAPI: <http://localhost:8100/docs>
- Postgres: `localhost:5434` (user `shop`, senha `shop`, base `shop`)

Portas não-óbvias de propósito: 8080 e 9090 já estão ocupadas nesta máquina, e 5432/5433 costumam
ter Postgres local.

No primeiro boot a API espera o banco, cria o schema e roda o seed determinístico
(~400 produtos, 200 clientes, 300 pedidos históricos). Boots seguintes pulam o seed se já houver dados.

```bash
make up        # build + start
make ps        # status
make smoke     # valida que está de pé e se comportando
make down      # para (dados sobrevivem)
make clean     # para e apaga o volume do banco
```

---

## Autenticação

Toda rota `/v1/**` exige o header `X-API-Key`. Sem header → **401**; chave desconhecida → **403**.

| `api_key_id` | chave | tier | quota |
|---|---|---|---|
| `acme-retail` | `acme-retail-key` | enterprise | 6000/min |
| `bolt-shop` | `bolt-shop-key` | pro | 600/min |
| `quickcart` | `quickcart-key` | free | 60/min |
| `nightowl-bot` | `nightowl-bot-key` | free | 60/min |

Estourar a quota → **429** com `Retry-After`. Como o `api_key_id` vai em toda linha de log, um
consumidor barulhento aparece como uma única chave gerando todos os 429.

```bash
curl -H "X-API-Key: acme-retail-key" "http://localhost:8100/v1/products?page_size=3"
```

---

## Superfície

| Método | Rota | Notas |
|---|---|---|
| GET | `/health` | liveness, sem auth |
| GET | `/ready` | checa o banco, 503 se indisponível |
| GET | `/v1/products` | `?category=&q=&min_price=&max_price=&in_stock=&page=&page_size=` |
| GET | `/v1/products/categories` | lista as categorias existentes |
| GET | `/v1/products/{id}` | |
| POST | `/v1/customers` | 409 `duplicate_email` |
| GET | `/v1/customers/{id}` | |
| GET | `/v1/customers/{id}/orders` | paginado |
| POST | `/v1/orders` | valida e debita estoque; 409 `insufficient_stock` |
| GET | `/v1/orders` | `?status=&customer_id=&since=&page=` |
| GET | `/v1/orders/{id}` | inclui os itens |
| POST | `/v1/orders/{id}/pay` | 402 `payment_declined` |
| POST | `/v1/orders/{id}/ship` | |
| POST | `/v1/orders/{id}/deliver` | |
| POST | `/v1/orders/{id}/return` | só a partir de `delivered` |
| POST | `/v1/orders/{id}/cancel` | devolve o estoque |

### Máquina de estados do pedido

```
pending ──pay──▶ paid ──ship──▶ shipped ──deliver──▶ delivered ──return──▶ returned
   │               │
   └──cancel──▶ cancelled ◀──cancel──┘
```

Qualquer transição fora desse grafo retorna **409 `invalid_status_transition`**. Pagar duas vezes o
mesmo pedido é 409, não 402 — a alcançabilidade é checada antes da regra de pagamento.

### Erros

Todos com a mesma forma:

```json
{"error": {"code": "insufficient_stock", "message": "...", "request_id": "…", "details": {...}}}
```

Códigos: `missing_api_key` (401), `invalid_api_key` (403), `payment_declined` (402), `not_found` (404),
`duplicate_email` / `insufficient_stock` / `invalid_status_transition` (409), `validation_error` (422),
`rate_limit_exceeded` (429), `internal_error` (500).

### Regras determinísticas

Nada aqui é aleatório — mesma entrada, mesma resposta, sempre.

- **Pagamento recusado** quando o `payment_token` começa com `tok_decline` **ou** o total passa de
  `PAYMENT_DECLINE_ABOVE_CENTS` (default R$ 5.000,00).
- **Estoque insuficiente** quando a quantidade pedida excede o disponível. Um produto em cada oito
  nasce com estoque entre 0 e 3, então isso acontece naturalmente.
- **Rate limit** por chave, janela deslizante de 60s.

---

## Os dois sinks de log

### 1. JSON-lines em arquivo — `/var/log/app/api.jsonl`

Uma linha JSON por request, escrita dentro do container e bind-montada em `./logs/` no host.
`RotatingFileHandler` com 10 MB × 5 arquivos, para não crescer sem limite. Tudo também vai para
stdout, então `docker compose logs` mostra o mesmo conteúdo.

```bash
make tail      # do host
make applog    # de dentro do container
```

```json
{"ts":"2026-08-06T14:03:11.482Z","level":"INFO","service":"ecommerce-api","logger":"access",
 "event":"http_request","msg":"http_request","request_id":"6f1c…","method":"POST",
 "path":"/v1/orders","route":"/v1/orders","status":201,"latency_ms":47.3,
 "api_key_id":"acme-retail","tier":"enterprise","user_agent":"traffic-gen/1.0",
 "client_ip":"172.18.0.1","order_id":"…","customer_id":"…","amount_cents":18990}
```

Campos sempre presentes: `ts`, `level`, `service`, `event`, `request_id`.
Em linhas de acesso: `method`, `path`, `route`, `status`, `latency_ms`, `api_key_id`, `tier`.
Em erros: `error_code` e, nos 500, `error_class` e `exc` com o traceback.

`route` é o template (`/v1/orders/{order_id}`), não o path concreto — sem isso, agrupar log por
endpoint seria impossível, já que cada id viraria uma rota própria.

### 2. Tabela `domain_events` no Postgres

Log **de negócio**, gravado na mesma transação da mutação que o causou — evento e estado nunca
divergem.

```bash
make events    # contagem por tipo
make psql      # shell SQL
```

```sql
SELECT event_type, count(*)
FROM domain_events
WHERE ts > now() - interval '5 minutes'
GROUP BY 1 ORDER BY 2 DESC;
```

Colunas: `id`, `ts`, `event_type`, `aggregate_type`, `aggregate_id`, `request_id`, `api_key_id`,
`payload` (JSONB).

Vocabulário fechado de eventos: `customer.registered`, `order.created`, `order.paid`,
`order.payment_declined`, `order.cancelled`, `order.returned`, `stock.depleted`,
`stock.insufficient`, `ratelimit.exceeded`.

### Correlação

O header `X-Request-Id` enviado pelo cliente é ecoado na resposta, gravado em toda linha do
`api.jsonl` e na coluna `domain_events.request_id`. É a chave que liga os três lados: cliente,
log técnico e evento de negócio.

```sql
SELECT * FROM domain_events WHERE request_id = '<id>';
```

---

## Configuração

Tudo por variável de ambiente; ver `.env.example` para a lista completa com defaults.
As que mais importam:

| Variável | Default | Efeito |
|---|---|---|
| `API_PORT` | `8100` | porta no host |
| `POSTGRES_PORT` | `5434` | porta do banco no host |
| `LOG_LEVEL` | `INFO` | `DEBUG` para ver tudo |
| `SEED_ON_START` | `true` | popula só se o banco estiver vazio |
| `RESET_ON_START` | `false` | **destrutivo**: dropa e recria o schema no boot |
| `PAYMENT_DECLINE_ABOVE_CENTS` | `500000` | teto acima do qual o pagamento é recusado |
| `API_KEYS` | ver tabela | formato `id:chave:tier`, separado por vírgula |

Para repopular do zero: `make clean && make up`.

---

## Estrutura

```
app/
├─ main.py            wiring: lifespan, handlers de erro, middleware, rotas
├─ config.py          settings via env
├─ db.py              engine, sessão, wait_for_db, create_all
├─ models.py          Customer, Product, Order, OrderItem, DomainEvent + transições
├─ schemas.py         contrato Pydantic (é o que aparece em /docs)
├─ auth.py            chaves de API, tiers, rate limiter
├─ deps.py            dependência require_api_key
├─ context.py         ContextVar de request: request_id, api_key_id, campos extras
├─ middleware.py      correlação, cronômetro, linha de access log
├─ logging_setup.py   JsonFormatter + RotatingFileHandler
├─ events.py          emit() -> domain_events + espelho no log
├─ errors.py          hierarquia de erro e códigos estáveis
├─ util.py            validação de UUID antes do banco reclamar
├─ seed.py            dataset determinístico (seed fixo = 42)
└─ routers/           health, products, customers, orders
```

Sem Alembic de propósito: é um mock, o schema nasce de `create_all` no boot. Migração versionada
seria peso morto aqui.
