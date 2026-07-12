# v4 Cloud Run deployment

This guide is intentionally IAM-first. Do not deploy the API with
`--allow-unauthenticated` and do not make the reports bucket public.

## 1. Create identities and storage

Create three service accounts:

- `gcs-search-api`: creates tasks, reads/writes job documents, and signs
  report download URLs.
- `gcs-search-worker`: reads approved source buckets/tables, writes reports,
  and updates job state plus the BigQuery search cache.
- `gcs-search-tasks`: the only principal allowed to invoke the worker.

Create a private, uniform-bucket-level-access reports bucket. Configure a
lifecycle rule to delete reports after the organization-approved retention
period (for example 7–30 days).

Keep the `gcs-search-scope-policies` JSON and optional
`gcs-search-smtp-password` in Secret Manager, and grant each runtime service
account access only to the secrets it consumes. The policy is
an administrator-owned allowlist mapping a user-facing `scope_id` to fixed
GCP project, approved bucket/prefix roots, DAG table, inventory table,
exclusions, and allowed copy targets. The browser may submit several bucket +
path rows, but every row must remain within one of those approved roots.
Never accept BigQuery identifiers or unapproved source/copy bucket roots from
the browser. Email recipients are limited to the approved organization domain
list in `GCS_SEARCH_ALLOWED_EMAIL_DOMAINS`.

## 2. IAM least privilege

Grant at the narrowest resource level possible:

| Principal | Required access |
|---|---|
| API service account | Cloud Tasks Enqueuer on its queue; Firestore user; report-bucket object viewer; permission to call `signBlob` for the signing service account. |
| Worker service account | Firestore user; report-bucket object creator; object viewer only on approved source buckets; BigQuery Job User plus reader for approved DAG/inventory tables and editor for its dedicated cache dataset. |
| Task dispatcher service account | Cloud Run Invoker on **only** `gcs-search-worker`. |
| Individual users / Google Groups | IAP HTTPS Resource Accessor on the load-balancer IAP resource. |

The API and worker must use distinct service accounts. The API must not have
source-bucket or BigQuery data-table read permission.

## 3. Deploy services and queue

Build from the standalone project directory:

```bash
gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/REPOSITORY/gcs-search \
  --project PROJECT .

gcloud run services replace cloudrun-api.yaml \
  --image REGION-docker.pkg.dev/PROJECT/REPOSITORY/gcs-search \
  --region REGION --project PROJECT

gcloud run services replace cloudrun-worker.yaml \
  --image REGION-docker.pkg.dev/PROJECT/REPOSITORY/gcs-search \
  --region REGION --project PROJECT
```

Replace placeholders or render the manifests through Terraform/Cloud Deploy;
never commit real project IDs, bucket names, or policy JSON. Create a Cloud
Tasks queue with a finite dispatch rate and concurrency ceiling. Start small
(for example, 4 concurrent tasks) and raise it only after measuring GCS,
BigQuery, memory, and report-generation cost.

Cloud Tasks must use an OIDC token whose audience is the worker URL. Grant the
task dispatcher service account `roles/run.invoker` on the worker. Do not give
that role to users, the API service account, or `allUsers`.

## 4. Put the API behind IAP and the VPN boundary

1. Create a regional/serverless NEG and HTTPS load balancer pointing to the
   API Cloud Run service.
2. Enable IAP on the load-balancer backend; grant access to either individual
   users or the Google Group that requested the application.
3. Apply a Cloud Armor policy to the load balancer that permits only the
   corporate VPN egress CIDRs, then denies all other sources. If the company
   uses an internal Application Load Balancer instead, enforce the equivalent
   VPN/VPC route policy there.
4. Confirm the Cloud Run API ingress remains
   `internal-and-cloud-load-balancing`; direct service-URL traffic must fail.

The API trusts `X-Goog-Authenticated-User-Email` only in this topology. IAP
is responsible for user/group IAM; the application then enforces report/job
ownership per authenticated email address.

## 5. Operational controls

- Start with Cloud Run worker `containerConcurrency: 1` because a search job
  can consume substantial memory and outbound connections. Horizontal Cloud
  Run instances provide job concurrency; raise per-container concurrency only
  after load tests with representative file counts and report sizes.
- Every content or filename job writes an administrator-only cache access record to BigQuery. A
  repeated source definition reuses cached results when its GCS metadata
  manifest is unchanged; changed/new/deleted objects create a new cache
  snapshot and only changed/new objects are re-evaluated.
- Filename searches list all objects under the approved roots regardless of
  extension, but download no object data. Size and creation/update timestamps
  come from GCS metadata.
- Monitor queue depth, task failures, object-download errors, BigQuery bytes
  processed, cache hit rate, refresh-file count, and report size. Alert on
  failures or unexpectedly low cache hit rates.
- Apply a retention policy to the `gcs_search_cache` dataset. It stores search
  terms, source paths, hashes, line numbers, and match tokens; it does not
  store full source text.
- Test a user outside the IAP group, outside the VPN, a user accessing another
  user's job ID, an expired signed URL, and a direct worker invocation.

## Kubernetes equivalent

The application is portable, but Kubernetes is only preferable if your team
already operates it. Run the API and worker as distinct Deployments; set the
worker Pod concurrency to one; use Workload Identity, a private ingress with
IAP/OIDC and VPN policy, Cloud Tasks or a managed queue, Firestore, and the
same private reports bucket. Do not collapse API and worker into one shared
deployment.
