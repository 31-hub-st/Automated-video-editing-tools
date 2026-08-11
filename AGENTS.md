# StoryForge repository handoff

This private repository is the authoritative source for StoryForge code, release
automation, and recovery instructions. A new Codex session or a replacement
computer must read this file and `docs/NEW_MACHINE_RECOVERY.md` before changing
or deploying anything.

## Non-negotiable data rules

- Never edit, merge, replace, delete, or migrate a live StoryForge SQLite file
  by hand.
- Never put a database, `.sfbak`, API key, password, employee media, rendered
  video, or production output in the Git tree.
- `StoryForge-Hub-Latest.sfbak` is stored only as an asset on the private
  prerelease tag `hub-state-latest`. Repository access therefore grants access
  to the Hub business data. The repository must remain private.
- A restore is authoritative replacement, not a merge. Existing Hub data is
  rejected unless the operator explicitly passes `-ReplaceExistingData`.
- Windows DPAPI-protected provider secrets do not transfer to a different
  computer. Re-enter API keys after migration.

## Supported deployment path

1. Authenticate GitHub CLI to an account with access to
   `31-hub-st/Automated-video-editing-tools`.
2. Clone this repository.
3. For a replacement Hub, run an elevated PowerShell:

   ```powershell
   .\scripts\bootstrap_storyforge.ps1 -Role Hub -RestoreHubData -ReplaceExistingData
   ```

4. For an employee computer, run:

   ```powershell
   .\scripts\bootstrap_storyforge.ps1 -Role Employee -InstallRoot D:\StoryForge
   ```

5. Verify with `scripts/verify_storyforge_deployment.ps1`.

The bootstrap script downloads the latest stable application Release, verifies
the GitHub asset digest, sidecar manifest, archive SHA-256, size, and internal
`storyforge-update.json`, then installs to an ASCII-only fixed path. Hub mode can
also restore the single latest private Hub snapshot, register the logon task,
create a Private-network/LocalSubnet firewall rule, and verify health.

## Before replacing a Hub

Publish one fresh, validated snapshot from the old Hub computer:

```powershell
.\scripts\publish_hub_snapshot.ps1 -HubRoot D:\StoryForgeHub
```

The release always contains exactly two replaceable assets and does not
accumulate dated backups. Employee video/music folders, output files, caches,
and provider secrets are intentionally excluded.

The publisher never infers backup CLI support from `current.json`. By default it
uses the repository CLI with Python 3.11 or 3.12; an explicit `-PythonExe` is allowed, and
`-StoryForgeExe` is only for an executable whose CLI support has been verified.
Never bypass the private-repository, digest, manifest, offline
restore-result, exact-version health, listening-process, or DataRoot safety
checks to make a migration appear successful.

## Verification before release or deployment changes

Run the focused contract tests and PowerShell parser checks described in
`docs/NEW_MACHINE_RECOVERY.md`. Do not claim a deployment is complete until the
checks pass. Do not start, stop, or replace the production Hub unless the user
explicitly asks for that operation.
