# DriveBatch deployment

## Cloud Run deployment

Cloud Run is the recommended deployment target when the service needs more memory
than Render's free plan. The repository includes a `Dockerfile`, `cloudbuild.yaml`,
and `cloud-run.yaml`.

1. Create or select a Google Cloud project and enable billing.
2. Enable Cloud Run, Cloud Build, Artifact Registry, Secret Manager, and Cloud
   Storage APIs.
3. Create an Artifact Registry Docker repository named `drivebatch` in
   `europe-west3`.
4. Create a private Cloud Storage bucket and set its name as `GCS_BUCKET`.
5. Create Secret Manager secrets named `google-client-secret-json`,
   `drivebatch-secret-key`, `cloudconvert-api-key`, and `google-api-key`.
6. Build the image:

   ```text
   gcloud builds submit --config cloudbuild.yaml
   ```

7. Replace `IMAGE_URI`, `REPLACE_WITH_BUCKET_NAME`, and
   `REPLACE_WITH_PUBLIC_CALLBACK_URL` in `cloud-run.yaml`, then deploy:

   ```text
   gcloud run services replace cloud-run.yaml --region=europe-west3
   ```

The Cloud Run service account needs `Storage Object Admin` on the bucket and
`Secret Manager Secret Accessor` on the four secrets. The service is currently
limited to one instance because job status is still held in process memory.
Cloud Storage preserves generated ZIPs and compressed files when the instance
restarts. A later Redis or database migration can remove the one-instance
limit.

`GOOGLE_API_KEY` is optional. If configured, users can scan and download files
from folders shared as "Anyone with the link" without signing in. Private Drive
folders, compression, and saving results back to Drive still require OAuth.

## Custom domain and OAuth

After deployment, map the purchased domain in Cloud Run. Add the DNS records
shown by Google, then set:

```text
OAUTH_REDIRECT_URI=https://your-domain.example/oauth2callback
```

In Google Cloud OAuth settings, add the domain to authorized domains and add
the same callback URL to authorized redirect URIs. Set the consent screen
homepage to `https://your-domain.example/`, privacy policy to
`https://your-domain.example/privacy`, and terms URL to
`https://your-domain.example/terms`.

The privacy and terms pages are templates. Replace the generic wording with
your legal name, contact email, business location, retention policy, and any
analytics/cookie disclosures before public launch. Google OAuth verification
may be required because DriveBatch requests write access to Google Drive.
