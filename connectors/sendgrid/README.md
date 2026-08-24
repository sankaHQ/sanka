# SendGrid connector

Read-only Twilio SendGrid Marketing Contacts source bundled with
`sanka-migrate`.

The connector uses the current v3 Marketing Contacts export contract:

1. inspect the contact summary with `GET /v3/marketing/contacts`;
2. start an all-contact JSON export with `POST /v3/marketing/contacts/exports`;
3. poll `GET /v3/marketing/contacts/exports/{id}` until the export is ready;
4. download every signed export URL without forwarding the SendGrid API key;
5. page the immutable export by a persisted job id and offset.

The export job id is also the connector's high-water mark, so an apply or
resume reads the same provider snapshot instead of silently starting a newer
export. Expired export jobs fail closed and require a fresh plan.

Authentication uses a SendGrid API key in `Credentials.access_token`. The API
origin defaults to `https://api.sendgrid.com`; contract-compatible isolated
environments may set `Credentials.settings["api_base_url"]`.

The connector is Apache-2.0 and imports only the bundled `sanka.connector`
interface plus `httpx`.

Provider references:

- https://www.twilio.com/docs/sendgrid/api-reference/contacts/get-sample-contacts
- https://www.twilio.com/docs/sendgrid/api-reference/contacts/export-contacts
- https://www.twilio.com/docs/sendgrid/api-reference/contacts/export-contacts-status
