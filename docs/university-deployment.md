# Wednesday university demo: publication and hosting

The selected deadline approach is now [persistent local hosting with Tailscale
Funnel](tailscale-demo.md). The Render recipe below is a historical, inactive
alternative; its resettable storage was not approved.

Prepared October 13, 2026. **Not deployed; Groq is not accepted as default.**
`render.yaml` is an inactive recipe, using JSON syntax (valid YAML). Creating a
Blueprint starts an initial deployment even with auto-deploy off. Do not create
it before the gates below pass. Local Ollama defaults and the real `.env` remain
unchanged. No credentials or usable accounts are included in the recipe.

## Recommended temporary cloud option

One Render **Free** Docker service: existing Starlette/static frontend, ONNX
MiniLM, unchanged JSON index, Gemini routing and explicitly configured Groq.
This is a **resettable, fictional-data-only demo**, not durable production.

Official sources checked October 13, 2026:

- [Free services](https://render.com/docs/free): HTTPS, 750 monthly instance hours,
  sleep after 15 idle minutes, roughly one-minute wakeup; no persistent disks,
  shell or one-off jobs. Files are lost on sleep, restart and redeploy. Without a
  payment method, exhausted included bandwidth suspends services; exhausted build
  minutes disables builds. Do not add billing/payment methods or upgrade plans.
- [Compute plans](https://render.com/docs/compute-plans): Free has 512 MB RAM.
- [Docker](https://render.com/docs/docker): variables are passed as build arguments.
  Only the non-secret `EMBEDDING_PROVIDER` build argument selects ONNX here.
  Never declare API keys as Docker ARG/ENV, echo them or copy `.env` into images.
- [Blueprint reference](https://render.com/docs/blueprint-spec): `sync: false`
  prompts privately for values; `autoDeployTrigger: off` disables later auto-deploys.
- [Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-overview): new
  Docker/Gradio compute Spaces currently require a paid plan. Older free-Docker
  tutorials are not a reliable $0 option for a new account.

Confirm free-plan account eligibility in the provider UI. Stop if paid enrollment
is required. No account, remote Blueprint validation or deployment was performed.

## Persistence requirements

Keep one process/replica. Administrative transactions attach SQLite databases;
network filesystems/object-store mounts are not a safe substitute for local disk.

| State below `KNOWVIA_DATA_DIR` | Lost on an ephemeral reset |
| --- | --- |
| `accounts.db` | Accounts and password hashes; login must be provisioned again |
| `authority.db` | Admin/employee roles, sanitized admin audit, drafts/version pointers |
| `employee_directory.db` | Activation changes and added records; fictional seed returns |
| `authenticated_sessions.db` | Sessions; everyone is logged out |
| `conversations.db` | Transcripts, titles, pins and conversation metadata |
| `pending_actions.db` | Pending confirmations; old proposals cannot authorize execution |
| `ticket_lifecycle.db` | Mock tickets and lifecycle/status history |
| `agent_audit.jsonl` | Local agent audit history |
| `knowledge/versions/` | Uploaded Markdown, published indexes/manifests and rollback files |
| Runtime `knowvia_index.json` | Copied again from the unchanged image seed |

Bundled fictional documents and pinned ONNX assets remain in image layers.
Docker uses `/var/lib/knowvia` for mutable state, `/opt/knowvia/knowledge` for
reference documents and `/opt/knowvia/bootstrap` for the seed index. The free
Blueprint configures **no disk**. Never promise cross-restart persistence, use
real employee information, or publish runtime databases/uploads/backups.

A durable complete demo needs one host with a durable local volume and private
terminal. University-provided hosting is a possible $0 option only if those
resources are supplied. The existing local HTTPS tunnel is a temporary fallback
requiring the laptop to stay online. Neither is an independently verified free
cloud entitlement. PostgreSQL would require an application migration and would
not by itself persist uploaded document/version files; it is not a deadline shortcut.

## Mandatory gates

1. Finish Groq acceptance and inspect remaining quality failures. This recipe
   must not bypass acceptance or silently use Ollama in the cloud.
2. Decide whether ephemeral resets are acceptable. No keep-alive tricks,
   public database backups, default passwords or credentials in image layers.
3. Resolve admin provisioning: `python -m knowvia.admin` requires a private
   interactive terminal and hidden password input. Render Free has no shell.
   **The unchanged app cannot bootstrap an admin there.** Registration stays
   disabled; demo-directory seeding does not create accounts/admin authority.
   Choose a host with a private terminal, or separately authorize/review a narrow
   server-secret bootstrap adapter. No adapter is implemented here. Ephemeral
   resets would need secure reprovisioning; session loss is not backup/restore.
4. Test the actual ONNX Linux container under 512 MB, including authenticated
   chat, provisioning and knowledge publication. The isolated Windows probe
   measured peak RSS 287,944,704 bytes (~275 MiB), with no Torch/Sentence
   Transformers imports. This is **not** a Linux fit/concurrency guarantee.

Offline container checks, requiring Docker and build downloads but no AI API calls:

```text
docker build --build-arg EMBEDDING_PROVIDER=onnx -t knowvia-demo .
docker run --rm --memory=512m --entrypoint python knowvia-demo -m knowvia.embedding_memory --directory /opt/knowvia/model-cache/onnx-minilm
python -m knowvia.repository_safety
```

## Publication and eventual deployment order

1. Review Git status/diff. Stage only intended source/tests/admin assets/docs and
   templates. Never force-add ignored runtime or saved evaluation files.
2. Run safety/offline tests and review the fictional NovaTech knowledge. The
   immutable matrix/benchmark remain source; saved results stay under ignored
   `artifacts/`. Choose a LICENSE before inviting reuse (none currently exists).
3. Only after explicit approval: create the GitHub repository, review staged
   files, commit and push. No command in this task performs those actions.
4. After all hosting gates and separate deployment approval, create the Free
   Render Blueprint. Do not add a disk, paid plan or payment method.
5. Set `KNOWVIA_ALLOWED_HOSTS` to the assigned `name.onrender.com` plus
   `localhost,127.0.0.1` for health checks. No wildcard hosts. Enter only
   `GEMINI_API_KEY` and `GROQ_API_KEY` privately in the platform secret UI.
6. The image runs `python -m knowvia.production`: readiness/index bootstrap,
   lightweight `/health`, then one Uvicorn worker on platform `PORT`.
   `FORWARDED_ALLOW_IPS=*` is safe **only** with a container reachable exclusively
   through Render's trusted HTTPS edge. It preserves HTTPS Origin checks/Secure
   cookies; never reuse it on a directly reachable server trusting arbitrary
   forwarded headers. Verify this boundary on the actual deployment.
7. Provision accounts by the separately approved method. Verify `/`, `/admin`,
   login/logout, permissions, sources, cancellation/confirmation and mock-only
   ticketing over HTTPS. Live model checks require separate authorization.
8. Give the teacher credentials privately and an explicit reset warning.
   No sensitive data should be entered. Uploads and admin edits are temporary.

Local setup remains `python -m pip install -e ".[dev,gemini]"`, an ignored `.env`
based on `.env.example`, and `python -m knowvia.web`. The optional Gemini generation
path is preserved; the recipe retains the existing Gemini routing model/settings.
