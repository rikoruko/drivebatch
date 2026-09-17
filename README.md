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

`GOOGLE_API_KEY` is required. Users can scan and download files from folders
shared as "Anyone with the link" without signing in. DriveBatch does not request
Google OAuth access, access private Drive folders, or save results back to Drive.

## Custom domain

After deployment, map the purchased domain in Cloud Run and add the DNS records
shown by Google. DriveBatch does not use Google OAuth.

The privacy and terms pages are templates. Replace the generic wording with
your legal name, contact email, business location, retention policy, and any
analytics/cookie disclosures before public launch.
