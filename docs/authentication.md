# RootED authentication configuration

RootED uses one OIDC callback shape for Google and Microsoft:

- Google: `https://<host>/auth/callback/google`
- Microsoft: `https://<host>/auth/callback/microsoft`

Register the matching `http://localhost:<port>` callback separately for local
development. Redirect URIs must match exactly.

## Environment variables

- `SECRET_KEY`: long random production session secret
- `ENABLE_GOOGLE_AUTH=true`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `ENABLE_MICROSOFT_AUTH=true`
- `MICROSOFT_CLIENT_ID`
- `MICROSOFT_CLIENT_SECRET`
- `MICROSOFT_TENANT`: `common` by default; use a tenant ID to restrict sign-in
- `ALLOW_SSO_AUTO_CREATE=true`: creates pending, restricted RootED accounts after valid OIDC login

The legacy `MS_CLIENT_ID` and `MS_CLIENT_SECRET` names remain accepted during
deployment migration. Prefer the `MICROSOFT_*` names for new configuration.

For Microsoft multi-tenant access, register the app as supporting organizational
accounts in any Microsoft Entra ID tenant and use `MICROSOFT_TENANT=common`.
Configure the web redirect URI above and create a client secret. Google requires
an OAuth web client with its corresponding authorized redirect URI.

Authentication creates or resolves an account with `account_role=pending`; it
grants no teacher, student, owner, class, or subscription authority. The legacy
`users.role=student` value remains only as a temporary database compatibility
field and is not used to authorize pending accounts. Owner access requires an active
`user_platform_roles` grant. Teacher access requires the teacher role plus an
active class section. Student instruction requires a linked roster student plus
enrollment in an active class. Everyone else is sent to `/restricted`.

## Migration and rollback

`migrations/add_user_auth_identities.py` provides both `migrate(conn)` and
`downgrade(conn)`. Rollback is deliberately refused while pending accounts or
multiple active identities exist, because the legacy schema cannot represent
those records without data loss. Once those conditions are resolved, downgrade
copies the remaining identity into the legacy SSO columns and removes the
provider-neutral table and account-role column.

## Production deployment checklist

Complete this checklist separately for development, staging, and production.
Redirect URIs must exactly match the scheme, hostname, port, path, and trailing
slash behavior used by that environment.

### Google

- [ ] Create or select the RootED project in Google Cloud Console.
- [ ] Configure the OAuth consent screen and required application information.
- [ ] Choose the appropriate internal or external audience.
- [ ] Add the production authorized redirect URI:
      `https://rooted.school/auth/callback/google`
- [ ] Add the exact localhost authorized redirect URI, typically:
      `http://localhost:5000/auth/callback/google`
- [ ] Create an OAuth 2.0 Web application client.
- [ ] Copy the Client ID into the deployment secret store.
- [ ] Copy the Client Secret into the deployment secret store.
- [ ] Configure `GOOGLE_CLIENT_ID`.
- [ ] Configure `GOOGLE_CLIENT_SECRET`.
- [ ] Configure `ENABLE_GOOGLE_AUTH=true`.
- [ ] Confirm no credentials or local `.env` files are committed.

### Microsoft

- [ ] Register the RootED application in Microsoft Entra.
- [ ] Select the supported account types.
- [ ] For Microsoft 365 Education across organizations, select accounts in any
      organizational directory.
- [ ] Add a Web platform configuration.
- [ ] Add the production redirect URI:
      `https://rooted.school/auth/callback/microsoft`
- [ ] Add the exact localhost redirect URI, typically:
      `http://localhost:5000/auth/callback/microsoft`
- [ ] Create a client secret and record its **Value**, not its Secret ID.
- [ ] Copy the Application (client) ID into the deployment secret store.
- [ ] Configure `MICROSOFT_CLIENT_ID`.
- [ ] Configure `MICROSOFT_CLIENT_SECRET`.
- [ ] Configure `MICROSOFT_TENANT=common` for multi-tenant school access.
- [ ] Configure `ENABLE_MICROSOFT_AUTH=true`.
- [ ] Confirm whether tenant administrator consent is required by target
      schools or districts.
- [ ] Confirm no credentials or local `.env` files are committed.

### RootED application

- [ ] Configure a long, random production `SECRET_KEY`.
- [ ] Set `ALLOW_SSO_AUTO_CREATE=true` only when self-registration is intended.
- [ ] Configure HTTPS and secure session-cookie behavior in production.
- [ ] Confirm the application generates HTTPS callback URLs behind the
      production proxy or hosting platform.
- [ ] Apply the authentication identity migration.
- [ ] Back up the production database before applying the migration.
- [ ] Confirm the restricted onboarding page is available at `/restricted`.

### Verification

- [ ] Google sign-in starts and returns through the registered callback.
- [ ] Microsoft sign-in starts and returns through the registered callback.
- [ ] A new SSO account is created with `account_role=pending`.
- [ ] A pending account is redirected to `/restricted`.
- [ ] A pending account cannot open `/dashboard` directly.
- [ ] A pending account cannot open `/student` directly.
- [ ] A pending account cannot open teacher, diagnostic, or instructional routes.
- [ ] A teacher with an active class can open the teacher dashboard.
- [ ] A teacher without an active class is redirected to restricted onboarding.
- [ ] A student with active class enrollment can open the student experience.
- [ ] A student without active enrollment is redirected to restricted onboarding.
- [ ] An owner with an active owner grant can open `/owner` without class membership.
- [ ] A non-owner receives a forbidden response from owner routes.
- [ ] Logout clears the RootED session.
- [ ] External post-login redirect destinations are rejected.
- [ ] Existing legacy Google-linked accounts still resolve to their original
      RootED accounts.
- [ ] Authentication failures do not disclose tokens, secrets, or provider
      error details to end users or logs.
