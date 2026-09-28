# Antigravity enterprise acceptance container

Replaces Gemini CLI as the candidate runtime for Gemini Enterprise Plus.
Version 1.2.12, Linux amd64; archive SHA512 is pinned in Dockerfile.

No project ID, region, license ID, API key or account is baked into the image.
Use account-based Business account / Google Cloud SSO. Select the existing
assigned license in the login flow. The current test account uses <ge-license>;
this is a test selection, not a provider-wide setting.

Official references:
- https://antigravity.google/docs/enterprise
- https://antigravity.google/docs/cli/install/
- https://antigravity.google/docs/cli/headless/

Test host: <deploy-user>@<test-host>
Container: antigravity-provider-probe
Login from an SSH terminal:

    docker exec -it -e SSH_CONNECTION="$SSH_CONNECTION" antigravity-provider-probe agy

SSH_CONNECTION is explicitly forwarded because Docker exec does not inherit the
SSH host's environment. Follow the remote URL/code flow in your own terminal.
Choose Business account, Continue with Google Cloud, then the assigned Plus
license. Do not choose a separate consumption-billing project.

Home and workspace use dedicated volumes. The container has no host port,
Docker socket, or connection to the gateway Compose network. The binary is on a
read-only root filesystem; CLI update checks may report it cannot self-update.

Formal integration must discover the account's available licenses at login and
persist the selected project, region and license per worker. Multiple licenses
require selection. Do not derive the project from an email domain or silently
fall back to a different project. License/project changes must invalidate
existing conversation bindings.

This is a login acceptance deployment, not a completed gateway adapter. The
existing Gemini ACP probe does not apply: Antigravity's documented headless
interface uses init/step_update/result JSONL, with cumulative session usage.
Tool bridging, cancellation, authentication persistence and subscription usage
still require real verification after login. Existing Codex workers are untouched.
