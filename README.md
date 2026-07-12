# GCS Search Macro

Standalone production service for concurrent content and filename searches across approved GCS bucket paths. It provides a browser UI and API, asynchronous Cloud Tasks workers, owner-isolated job state, BigQuery result reuse, Excel downloads, optional organization email delivery, and controlled copying to approved GCS targets.

This directory is a complete build context. It does not import, install, mount, or read code or data from another application version.

For the full guide — business use case, architecture, installation, configuration reference, API usage, and troubleshooting — see [DOCUMENTATION.md](DOCUMENTATION.md).

## Production topology

```text
Corporate VPN
    |
HTTPS load balancer + Cloud Armor + IAP (individual/group access)
    |
Cloud Run API -> Cloud Tasks -> Cloud Run workers
    |                              |
Firestore jobs                 GCS sources/copies
    |                           BigQuery cache/enrichment
private report bucket -> short-lived download URL
```

The browser UI is served by FastAPI; Streamlit is not used. Horizontal worker instances provide job concurrency, while each job also uses bounded concurrent GCS reads and optional copies.

## Behavior

- Users add/remove independent GCS bucket and path rows within administrator-approved roots.
- Users first choose whether to search for terms inside Python files or search
  GCS filenames. Filename mode checks every object extension using metadata
  only; it does not download object contents.
- Literal and safe-regex terms both report exact and partial occurrences.
- Filename results classify complete case-insensitive filename equality as
  exact and containment as partial. The workbook includes the original
  filename, full blob name, GCS URI, landed/created time, last-updated time,
  byte size, and MiB size.
- Every search access is recorded in BigQuery. An identical request reuses cached results when the GCS manifest is unchanged; only new or updated objects are rescanned, and deleted objects are removed.
- Optional copies are written below a job-specific path in an approved target.
- Reports can be downloaded or sent to up to five recipients in configured organization domains.
- Cache and manifest maintenance are administrative and do not appear in the UI or workbook.

## Local development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
uvicorn gcs_search_macro_v4.api:app --reload
```

Set `APP_ENV=development` and use `X-Dev-User-Email` only for local requests. Production requires IAP identity headers and rejects the development identity path.

Build the standalone image from this directory:

```bash
docker build -t gcs-search .
```

See [deploy/README.md](deploy/README.md) for GCP resources, IAM, network controls, configuration, and deployment.

## BigQuery cache

The dedicated cache dataset contains:

- `search_cache_run`
- `search_cache_manifest`
- `search_cache_result`
- `search_cache_access`

The cache stores object metadata and match locations, never full object contents. Apply organization-approved dataset and report-bucket retention policies.

## License

Licensed under the [Apache License 2.0](LICENSE).
