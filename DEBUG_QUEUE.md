# Debug: recognize_llm backfill/queue failures (run on the PRODUCTION host)

**You (the assistant) are on the prod Nextcloud host (cloud.morill.es, NC 33.0.3, rootless podman).**
The exApp is deployed and enabled; uploads + backfill enqueue jobs into a SQLite queue inside the
exApp container, and a worker thread drains them (download image → llama.cpp caption → write system
tags + description). **Some queue jobs are failing and we need the root cause.**

Key fact: the failed rows showed **empty `error` strings**, so the queue table alone won't tell you
why. The fastest path is to **reproduce one job synchronously and read the traceback** (Step 2).

Containers: NC = `nextcloud-app`, exApp = `nc_app_recognize_llm`.
```bash
NC=nextcloud-app; EX=nc_app_recognize_llm
occ() { podman exec -u www-data "$NC" php /var/www/html/occ "$@"; }
```

---

## Step 1 — inspect the queue (note: include the `status` column!)
```bash
podman exec $EX sh -c 'cd /app/lib && python3 -c "
import job_queue as q, sqlite3
print(\"DB:\", q._DB)
print(\"status:\", q.status())
c=sqlite3.connect(q._DB)
print(\"--- rows (status,user,file,source,attempts,error) ---\")
for r in c.execute(\"SELECT status,user_id,file_id,source,attempts,substr(error,1,200) FROM jobs ORDER BY updated_at DESC LIMIT 20\"):
    print(r)
"'
```
First decide what you're actually looking at:
- `status='failed'` with a real `error` → read it, jump to the matching cause below.
- `status='failed'` with **empty** error → go to Step 2 (reproduce to get the traceback).
- `status='done'` rows that were **skipped** (e.g. wrong mimetype) are normal — skips are recorded as
  `done`, not failures. Confirm by reproducing (Step 2) — you'll see `RESULT: skipped reason=...`.

---

## Step 2 — reproduce ONE job and print the real error
Runs the exact engine path (`processor.process_file`) synchronously with full traceback. Replace
`user`/`file_id` with a failing `(user_id, file_id)` from Step 1.
```bash
podman exec $EX sh -c 'cd /app/lib && python3 -c "
import settings as s, processor, traceback
from nc_py_api import NextcloudApp
nc = NextcloudApp()
user, file_id = \"admin\", 105            # <-- from the queue
cfg = s.load(nc)
print(\"llama_url=\", cfg.chat_url, \"| model=\", repr(cfg.llama_model), \"| api_key set=\", bool(cfg.api_key))
print(\"mimetypes=\", cfg.mimetypes)
try:
    res = processor.process_file(nc, user, file_id, cfg, force=True)
    print(\"RESULT:\", res)
except Exception:
    traceback.print_exc()
"'
```
The traceback (or the `RESULT:`) names the failing layer. Map it:

| What you see | Cause | Fix |
|---|---|---|
| `RESULT: skipped reason=mimetype ...` | File isn't an allowed image type | Not a failure. Adjust `mimetypes` config if needed. |
| `RESULT: skipped reason=not a file` / `by_id` is None | File id gone, or wrong user context | Expected for deleted files; check the user actually owns it. |
| `VisionError ... 401 Unauthorized` | API key wrong/unset | `occ app_api:app:config:set recognize_llm api_key --value <key>` |
| `VisionError ... Connection refused / timeout` | exApp can't reach llama | See Step 3. |
| `VisionError ... did not return parseable JSON` | Model replied with prose | Tune the prompt (Admin → Additional → Recognize LLM) or model. |
| Error in `assign_tag` / PROPPATCH / `add_comment` | Write-back (tags/description) | Step 4. |
| `Session.request() ... unexpected keyword` or similar | Image is older than the DAV-fix build | Confirm the running image is current (`podman inspect $EX --format '{{.ImageName}}'`), redeploy. |

---

## Step 3 — verify llama.cpp reachability + the key (from the exApp's network)
```bash
# models list (usually open)
podman run --rm --network podman-default-kube-network docker.io/curlimages/curl \
  -s -m 8 http://192.168.0.143:11434/v1/models | head -c 300; echo
# a real completion WITH the key (replace <KEY>) — should be 200, not 401
podman run --rm --network podman-default-kube-network docker.io/curlimages/curl -s -m 30 \
  -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":5}' \
  http://192.168.0.143:11434/v1/chat/completions | head -c 300; echo
```
Get the configured values: `occ app_api:app:config:list recognize_llm` (api_key shows as
`***REMOVED***` — that only means it's set).

---

## Step 4 — test write-back in isolation (tags + description)
If captioning works but write-back fails:
```bash
podman exec $EX sh -c 'cd /app/lib && python3 -c "
from nc_py_api import NextcloudApp
import dav
nc = NextcloudApp(); nc.set_user(\"admin\")
node = nc.files.by_id(105)
print(\"node:\", node.user_path if node else None)
# tag
t = nc.files.tag_by_name(\"llmtest\") if any(x.display_name==\"llmtest\" for x in nc.files.list_tags()) else (nc.files.create_tag(\"llmtest\") or nc.files.tag_by_name(\"llmtest\"))
nc.files.assign_tag(node, t); print(\"tag OK\")
# dav property + comment
dav.set_props(nc, node, {dav.PROP_DESCRIPTION: \"debug write\"}); print(\"propset OK\")
print(dav.get_props(nc, node, [dav.PROP_DESCRIPTION]))
dav.add_comment(nc, node.info.fileid, \"debug comment\"); print(\"comment OK\")
"'
```

---

## After a fix — re-run the failed jobs
```bash
# Reset failed jobs to pending (worker picks them up):
podman exec $EX sh -c 'cd /app/lib && python3 -c "
import job_queue as q, sqlite3
c=sqlite3.connect(q._DB)
c.execute(\"UPDATE jobs SET status=\x27pending\x27, attempts=0, error=\x27\x27 WHERE status=\x27failed\x27\"); c.commit()
print(\"re-queued:\", c.total_changes)"'
# ...or wipe the queue entirely and re-backfill from scratch:
podman exec $EX sh -c 'cd /app/lib && python3 -c "import job_queue as q,sqlite3; c=sqlite3.connect(q._DB); c.execute(\"DELETE FROM jobs\"); c.commit(); print(\"cleared\")"'
```
Worker activity also logs to the **Nextcloud** log (not container stdout):
```bash
podman exec $NC sh -c 'tail -n 200 /var/www/html/data/nextcloud.log' | grep -i recognize_llm | tail
podman logs $EX 2>&1 | grep -Ei 'error|traceback|events/node|backfill' | tail
```

---

## Likely root cause (educated guess before you start)
The failed jobs were `source='event'` for `admin` files `105`/`111` with **empty errors and
attempts=1**. By the worker's retry logic a genuine *exception* would first go back to `pending`
(retry), not `failed` — so empty-error `failed@attempts=1` is suspicious. Two leads:
1. They may actually be **skips** miscounted, or rows from an earlier build. Confirm real `status`
   in Step 1 and reproduce in Step 2.
2. If they are true failures, the worker is storing an empty `str(e)` for some exception type —
   reproduce in Step 2 to see the real traceback, then we should harden `lib/job_queue.py` to store
   `repr(e)` + traceback (a small code fix worth pushing so future failures are self-explanatory).

Report back the Step 1 `status` line and the Step 2 traceback/RESULT and we can pinpoint it.
