# RootED authentication configuration

RootED uses one OIDC callback shape for Google and Microsoft:

- Google: `https://<host>/auth/callback/google`
- Microsoft: `https://<host>/auth/callback/microsoft`

Microsoft identity claims have deliberately separate purposes:

- `tid` plus `sub`: stable provider identity linkage only
- normalized `email`, then `preferred_username`, then `upn`: teacher email
  preauthorization matching, using only claims from Authlib's validated ID token
- `name`: display-only presentation

The provider subject is never used as an email or display-name fallback. A
Microsoft callback without an email-shaped trusted claim stops before creating
or linking a RootED account and shows a safe authorization error. The existing
`openid email profile` scopes already request the required claims; no Microsoft
Graph call or scope expansion is required.

Register the matching `http://localhost:<port>` callback separately for local
development. Redirect URIs must match exactly.

## Local environment file

For local development, copy `.env.example` to a file named `.env` in the
repository root:

`C:\Users\lrbro\OneDrive\Documents\RootED\.env`

RootED loads that file automatically through `python-dotenv`. Values already
present in the process environment take precedence over `.env`, so production
deployments continue to use the hosting platform's environment-variable or
secret-management settings. Do not deploy a `.env` file to production.

The local `.env` and all `.env.*` variants are ignored by Git, except for the
secret-free `.env.example`. Never place real credentials in `.env.example`.

## Environment variables

- `SECRET_KEY`: long random production session secret
- `ENABLE_GOOGLE_AUTH=true`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `ENABLE_MICROSOFT_AUTH=true`
- `MICROSOFT_CLIENT_ID`
- `MICROSOFT_CLIENT_SECRET`
- `MICROSOFT_TENANT`: `common` by default; use `organizations` to allow only
  work/school accounts, or a tenant ID to restrict sign-in to one organization
- `ALLOW_SSO_AUTO_CREATE=true`: creates pending, restricted RootED accounts after valid OIDC login

## Student onboarding and account administration

The normal student workflow is Google or Microsoft sign-in followed by an active
class join code. Teachers create and manage their own classes and share those
codes; they do not create global student records, login credentials, identities,
platform roles, or instructional authorizations.

Owners may create an exceptional local account from the Owner Account
Administration page for controlled testing, support, recovery, or exceptional
onboarding. Account identity, platform authority, instructional authorization,
and optional class membership are separate choices and are written in one
transaction. New local accounts never receive Owner authority implicitly.

Future roster pre-provisioning is intentionally deferred. A later design must
use class-scoped roster placeholders, server-generated internal student IDs, and
an explicit invitation or claim-linking flow. It must not link identities using
arbitrary name or email matching.

Owner impersonation is documented in
`docs/platform_roles_and_impersonation.md`. It never requests a target password:
the effective target identity is authorized normally while the acting Owner is
preserved in a server-side audit record. Logout always terminates impersonation.

Local maintenance credentials are never stored in source control. The
`fix_login.py` helper requires `ROOTED_MAINTENANCE_USERNAME` and
`ROOTED_MAINTENANCE_PASSWORD` in its process environment and never prints the
password. Set them only for the terminal session that runs the helper, then
close that terminal or remove the variables.

The legacy `MS_CLIENT_ID` and `MS_CLIENT_SECRET` names remain accepted during
deployment migration. Prefer the `MICROSOFT_*` names for new configuration.

For Microsoft multi-tenant access, register the app as supporting organizational
accounts in any Microsoft Entra ID tenant and use `MICROSOFT_TENANT=common`.
Configure the web redirect URI above and create a client secret. Google requires
an OAuth web client with its corresponding authorized redirect URI.

Microsoft's tenant-independent discovery endpoints (`common`, `organizations`,
and `consumers`) publish an issuer template containing `{tenantid}`, while each
ID token contains the concrete tenant ID in both `tid` and `iss`. RootED follows
Microsoft's documented validation procedure: it requires `tid` to be a GUID,
substitutes that value into the discovery issuer template, requires an exact
match with `iss`, and checks that the selected signing key is scoped to the same
issuer. Authlib continues to validate the signature, audience, nonce, expiry,
and other standard OIDC claims. A concrete tenant ID uses that tenant's
discovery document and Authlib's normal exact issuer validation.

Use `MICROSOFT_TENANT=organizations` when RootED should accept Microsoft 365
school/work accounts but not personal Microsoft accounts. Use `common` only
when the Entra app registration is intentionally configured to accept both
organizational and personal Microsoft accounts.

Authentication creates or resolves an account with `account_role=pending`; it
grants no teacher, student, owner, class, or subscription authority. The legacy
`users.role=student` value remains only as a temporary database compatibility
field and is not used to authorize pending accounts. Owner access requires an
active `user_platform_roles` grant. Teacher access requires an active
`user_instructional_authorizations` Teacher grant; a class is not required
because an authorized teacher must be able to enter the workspace and create
their first class. Student instruction requires a linked roster student plus
enrollment in an active class. Everyone else is sent to `/restricted`.

Post-login routing keeps these authorities independent: Owner accounts land at
`/owner` (including Owner + Teacher accounts), Teacher-only accounts land at
`/teacher`, enrolled Students land at `/student`, and accounts without an
applicable authorization or membership land at `/restricted`.

Owners manage account activation and Teacher instructional grants from People
& Access. Teacher revocation preserves grant history and is blocked while the
Teacher owns active classes. Account deactivation is reversible and preserves
identities, grants, memberships, and educational records. Teachers do not
manage platform accounts; they may archive only active memberships in classes
they own.

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
- [ ] Configure `MICROSOFT_TENANT=organizations` for multi-tenant school/work
      access, or `common` only when personal Microsoft accounts are also wanted.
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
- [ ] Confirm `.env` and `.env.*` are ignored and only `.env.example` may be
      committed.

### Verification

- [ ] Google sign-in starts and returns through the registered callback.
- [ ] Microsoft sign-in starts and returns through the registered callback.
- [ ] A new SSO account is created with `account_role=pending`.
- [ ] A pending account is redirected to `/restricted`.
- [ ] A pending account cannot open `/dashboard` directly.
- [ ] A pending account cannot open `/student` directly.
- [ ] A pending account cannot open teacher, diagnostic, or instructional routes.
- [ ] An account with an active Teacher authorization can open `/teacher`.
- [ ] An authorized teacher without a class can create their first class.
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
