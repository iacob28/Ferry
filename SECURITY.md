# Security

Ferry holds credentials for your storage and devices, so security reports are welcome.

## Reporting

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/iacob28/Ferry/security/advisories/new),
not as a public issue. You should get a reply within a week. Fixes are released as a new
image, and the advisory is published once the fix is out.

## Supported versions

Only the latest release gets fixes. Update with `docker compose pull && docker compose up -d`.

## Deployment notes

- Ferry is meant for a trusted home or office network. Do not expose the UI to the internet
  directly. Put it behind a VPN, or behind a reverse proxy with TLS and its own access control.
- The container runs with host networking, `NET_ADMIN`, `SYS_ADMIN` and access to `/dev`,
  because it joins camera Wi-Fi networks and mounts USB drives. Run it on a host dedicated
  to that job if you can, and drop what you do not need (see `docker-compose.yml`).
- Credentials are stored in `FERRY_DATA/settings.json`. The API never returns secrets, but
  anyone with access to that folder can read them. Keep it, and its backups, private.
