# Database for Holon

Give each Holon app its own PostgreSQL database and role. The provider identifies
the calling app from Holon’s workload identity. The `execute` tool runs a batch
of parameterized statements in one transaction.

## Add it to a Holon project

Declare `database` in the consuming app’s `sh.holon.requires` metadata in
`server.json`. Add the provider to the same `holon.yaml` as the calling app.
Replace `chat` in the caller list with your app’s name.

```yaml
- name: database
  registry:
    name: io.github.hughhan1/database
    version: "0.1.6"
  access:
    server:
      callers: [chat]
```

Set `DATABASE_HOST` and `DATABASE_USER` on the database app with
`holon config set`. Set `DATABASE_PASSWORD` with `holon secret set`.
Select the target installation and use `--env prod` for production values.
Deploy the consuming app with `holon deploy`. Holon finds the provider through
the registry, reads its tool contract, and starts it alongside the caller.

Use a dedicated PostgreSQL cluster or branch. The account must be able to create
roles and databases, then grant the provisions required by those roles. The
provider connects over TLS on port 5432 and verifies the server certificate.
Run the provider behind Holon so it receives the authenticated caller identity.
Keep direct access to the provider’s container port inside the platform’s
trusted network.

## Call the database

Pass one SQL statement per item and bind values by name. Holon generates
`apps.database.tools.execute` for apps that declare the dependency.

```ts
const { result } = await apps.database.tools.execute({
  key: requestId,
  statements: [
    {
      sql: "INSERT INTO messages (id, body) VALUES (:id, :body)",
      values: { id: messageId, body: text },
      expect: 1,
    },
    {
      sql: "INSERT INTO agent_jobs (message_id) VALUES (:id)",
      values: { id: messageId },
      expect: 1,
    },
  ],
});
```

A statement error or an `expect` mismatch rolls back the batch. For 24 hours,
a repeat using the same key returns the original committed results. Use a new
key for a different batch. Each statement returns column names, rows, and a
count of rows returned or changed. The provider accepts up to 100 statements per
call and caps each result at 10,000 rows and 4 MiB.

This release supports PostgreSQL 16 or later. The tools are intended for the app’s own
trusted server code. End users and agents should call the app’s domain tools.

## Verify locally

Use Python 3.15, uv, and a disposable PostgreSQL 18 instance on loopback. The
tests create isolated roles and databases. Set `TEST_DATABASE_URL` to the
disposable server’s admin URL if it differs from the test default.

```sh
uv sync --locked
uv run pytest
holon types generate --contract .
```
