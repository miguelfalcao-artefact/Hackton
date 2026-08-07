# API Diagnostic Classification

| Classification | Supporting signals | Important caveat |
|---|---|---|
| Capacity / resilience | Traffic spike, retry storm, high tail latency, 500s, pool exhaustion | Scaling requires saturation or load evidence |
| Abuse / rate-limit policy | Scraper or quota burst, 429, `rate_limit_exceeded` concentrated by identity | Correct enforcement is not misconfiguration |
| Business logic / concurrency | 409/402/422, stock/fraud event, idempotency or transaction evidence | Domain rejection alone is not a defect |
| Dependency / database | Timeout, refusal, DNS/TLS/auth error, empty configuration, pool failure | Preserve exact error semantics and ambiguity |
| Client / expected response | Isolated 404, invalid identifiers, ordinary validation, abandonment | Separate expected client behavior from server failure |

Use `Unclassified` when evidence is insufficient. Use `Mixed` only when two or more supported categories have comparable direct evidence.
