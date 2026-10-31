# Project rules

- Follow `docs/playbook.pdf`, `docs/playbook.md`, `docs/playbook_sesion.pdf`, and `docs/decisiones.md` in that order. If they conflict, stop and ask; do not choose.
- Work only in this workspace and one phase at a time. Wait for approval before starting the next phase.
- Do not invent or alter queries, resource profiles, CSV fields, versions, results, plans, or logs. Ask about unresolved decisions in `docs/decisiones.md` before depending on them.
- Do not run benchmarks, cache flushing, or the 500,000-record load. Small smoke tests require explicit approval for that session.
- Never write or fabricate rows in the results CSV. Every reported number must come from real CSV data.
- Keep Git on `main`; use short, imperative English Conventional Commit messages. Do not connect/create a remote or push without explicit approval. Never force-push or rewrite published history.
- Do not commit datasets, `.env` files, credentials, tokens, or secrets. Keep `results/` trackable.
- Do not install database or Docker MCPs. Run `gentle-ai doctor` read-only. Do not change PATH, remove duplicate installations, or repair/sync Engram without explicit approval.
