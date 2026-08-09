# FOMO Demo — submission notes

FOMO is an AI coding-agent workbench. A user describes an application, the agent plans and edits a real project inside OpenSandbox, deterministic verification checks the result, and the workbench exposes the live work log, generated code, terminal output, versions, and a runnable preview.

## Delivery links

- Online demo: pending public deployment
- Source repository: [github.com/hupicake/fomo-demo](https://github.com/hupicake/fomo-demo)
- Local runbook: [README.md](README.md)
- Technical design and trade-offs: [DESIGN.md](DESIGN.md)

The public source repository is available. The online demo remains a release
blocker; local success is not presented as evidence that the public deployment
is ready.

### Public-link completion gate

The intended evaluator topology is a named Cloudflare Tunnel with one
same-origin workbench host (`app.example.com`, where `/v1/*` routes to API and
all other paths route to Web) plus a cross-site generated-code host
(`*.fomo-previews.example.net`, routed to the verified Preview gateway). The
two hosts deliberately use different eTLD+1 sites. The exact template is
[`deploy/cloudflared/config.example.yml`](deploy/cloudflared/config.example.yml);
real Tunnel credentials remain outside Git.

Before replacing “pending public deployment” above:

1. Build Web with `NEXT_PUBLIC_API_URL=https://app.example.com`; run with
   `APP_ENV=production`, `WEB_ORIGIN=https://app.example.com`, and
   `PUBLIC_PREVIEW_BASE_DOMAIN=fomo-previews.example.net`.
2. Provision DNS and TLS for `*.fomo-previews.example.net`, preserve the
   incoming Preview Host, and bypass Cloudflare cache for the wildcard.
3. Expose only Cloudflare HTTPS. OpenSandbox `8080`, LiteLLM `4000`, Docker
   Preview ports including `40000-60000`, PostgreSQL, Redis, and MinIO must not
   be Internet-reachable.
4. Keep the named Tunnel and retained sandbox alive through the evaluation.
   Publication renews once for at most seven days; renew or rerun before expiry
   if the review window is longer.
5. From an external network, verify account login, one real generation and SSE
   work log, the final HTTPS Preview and `/_next/*`, interaction, and state
   after reload. Local Gateway/Playwright success does not satisfy this gate.

The Preview gateway supports ordinary HTTP Next.js pages for this demo, not
generated-app WebSocket, SSE, or arbitrary streaming transports.

## Implemented

- A real interactive flow from product request to generated code, deterministic verification, version creation, and live Preview.
- OpenSandbox-backed execution with Pi as the coding harness and full useful permissions inside `/workspace`; the sandbox, not a business-file allowlist, is the security boundary.
- GoalGraph planning through a schema-constrained virtual tool, a 200,000-token context window, reusable session context, and SSE work-log/status events.
- A fixed, independently scrollable work log plus Preview, Code, Terminal, and Versions workspaces. Frontend visual composition was delegated to WorkBuddy Hy3; backend and security contracts remain independent of the layout.
- PostgreSQL persistence for users, server sessions, projects, runs, events, artifacts, versions, and preview state. Redis and MinIO support runtime coordination and artifact storage.
- Minimal account flow: register, sign in, sign out, HttpOnly server session cookies, token rotation on authentication, guest-project transfer, cross-account project isolation, session revocation, and SSE authorization rechecks.
- Verified Preview retention and routing: a successful OpenSandbox Preview is renewed to seven days, then published through an isolated wildcard host gateway that resolves the current verified sandbox server-side. FOMO account cookies, authorization headers, and OpenSandbox credentials are not forwarded into generated applications.
- MetaGPT and frozen business-file plans are not part of the active runtime. The current path remains OpenSandbox; E2B is not required.

## Verified evidence

| Evidence level | Result | What it proves |
| --- | --- | --- |
| Backend code tests | 385 passed | Repository, API, auth/session, GoalGraph, worker, verifier, persistence, Preview retention, and publish contracts at code/integration level |
| Preview Gateway code tests | Passed within the 385-test backend suite | UUID Host validation, verified-resource lookup, header/cookie isolation, expiry handling, and bounded bodies; this does not exercise DNS, TLS, Tunnel, or a public network |
| Web Vitest | 134 passed | UI reducers, stores, API parsing, and components; this does not prove a browser flow |
| Pi bridge tests | 18 passed | Native/structured tool contracts, continuation, timeout/activity, user-input, and fail-closed protocol behavior without a live model |
| Automated Web Playwright suite | 2/2 local tests passed | Workbench smoke plus guest-project registration/rotation/migration, logout isolation, login recovery, and a second account denied by API (`403`) and UI |
| Real local PostgreSQL/API smoke | Passed | Guest-to-user token rotation, guest project transfer, logout revocation, and persisted account access against the running database |
| Real local OpenSandbox canary | `succeeded / ready`, verifier `verified` | Pi produced a runnable habit tracker through planning, building, verification, versioning, and Preview in the local OpenSandbox stack |
| Local Chrome/Playwright direct-Preview acceptance | Passed | Habit creation, completion toggle, summary updates, and localStorage state after reload through the direct local endpoint |
| Local Chrome/Playwright Gateway acceptance | Passed | The verified Preview, absolute `/_next` assets, interaction, and reload persistence work through the local host-based Gateway |
| Public HTTPS deployment acceptance | Not yet available | No claim is made until an external-network URL completes DNS/TLS/Tunnel, login, generation/SSE, Preview interaction, and reload |

The browser/runtime rows above used local endpoints or local Host routing; none
is a public HTTPS result. The successful OpenSandbox run took about 7 minutes
28 seconds: planning 63 seconds, building 5 minutes 29 seconds, and verification
50 seconds. Peak recorded context was 47,854 of 200,000 tokens. The generated
Preview remained healthy after verification and its retained sandbox was
extended to 2026-08-17 02:32 China Standard Time. Its only observed browser
noise on the earlier direct-origin check was a non-blocking missing
`favicon.ico` request.

## Key decisions

1. **Prioritize the complete vertical slice.** Account isolation, persistence, real generation, deterministic verification, and Preview take precedence over secondary dashboards and decorative proof views.
2. **Keep Pi native inside the sandbox.** FOMO adds goal/context/reuse strategy around the harness rather than reimplementing its coding loop or freezing a file plan.
3. **Use concentrated verification.** Low-cost targeted checks run during implementation; a small end-to-end matrix runs after a coherent module is complete.
4. **Be explicit about evidence.** Unit tests, a local OpenSandbox run, a browser check, and a public deployment are separate proof levels.
5. **Keep auth intentionally small.** Password reset, email verification, social login, organizations, billing, and admin screens are outside this demo.

## Known limits

- A stable public demo URL is still missing; the source repository is public.
- The current local OpenSandbox Docker configuration is suitable for a controlled demo, not unrestricted anonymous Internet traffic. A public release needs authenticated ingress, abuse/rate controls, and a hardened sandbox egress policy.
- Published Preview hostnames are high-entropy capability URLs. The gateway additionally requires a current, uncleaned verified resource in FOMO persistence, but it does not require the viewer's FOMO account after disclosure.
- `sessions.user_id` is validated by application logic but does not yet have a database foreign key. No public endpoint can create arbitrary identity links, so this is a follow-up migration rather than a demo blocker.
- The product advantage over an unmodified coding harness has not yet been established by a comparative benchmark. The current evidence proves a working delivery path, not model/context superiority.

## Next priorities

1. Configure a stable authenticated app/API ingress plus wildcard Preview DNS/TLS routed to the implemented gateway; keep the OpenSandbox lifecycle API and random Docker host ports private.
2. Replace the pending online-demo link, then rerun registration → generation → Preview → refresh from an external browser.
3. Add invite/rate limits and hardened egress controls before allowing untrusted public prompts.
4. Measure FOMO against the same model and sandbox with native Pi: completion rate, time to first write, time to verified Preview, repair count, and context-token use.
5. Improve component/recipe reuse only where benchmark data shows a repeatable delivery gain.
