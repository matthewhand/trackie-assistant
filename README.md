# Trackie Assistant

![Trackie Assistant banner featuring Trackie Dacks, the tracksuit-pants mascot wearing headphones](docs/trackie-assistant-banner.png)

[![CI](https://github.com/matthewhand/trackie-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/matthewhand/trackie-assistant/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Your Suno music, in your library.** Archive public playlists, profiles and songs
with verified producer names, artist tags and cover art — ready for Music Assistant.

> **Linux preview 1.6.2rc1** · Python 3.13+ · AMD64 containers/add-on<br>
> [Download the release](https://github.com/matthewhand/trackie-assistant/releases/tag/v1.6.2rc1) · [Release notes and limits](docs/releases/v1.6.2rc1.md)

## What you get

- **Audio and artwork:** M4A audio, embedded covers and separate cover images.
  Published video can supply audio or a still image; the temporary video is discarded.
- **Correct attribution:** filenames and artist tags use each track's verified producer.
- **Repeatable syncs:** saved clip IDs prevent duplicates; existing recordings are reused unchanged.
- **Visible progress:** queued tracks, estimated download size and destination free space.
- **Your choice of interface:** command line, local web UI/API or Home Assistant.

## Choose your setup

| Run it with | Start here |
| --- | --- |
| **Home Assistant** | [Add the repository](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmatthewhand%2Ftrackie-assistant), then follow the [add-on setup](addon/suno_dl/DOCS.md). |
| **Docker** | [Container setup](deploy/ct107/RUNBOOK.md). The release also includes the tested AMD64 image archive. |
| **CLI or local web UI** | Use the quick start below. |

Home Assistant starts with no sources selected and **Sync on start** off.
Choose your playlists before starting a download.

## Quick start

On Linux, install **Python 3.13+, FFmpeg and ffprobe**, then:

```bash
git clone https://github.com/matthewhand/trackie-assistant.git
cd trackie-assistant
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[tags]"

# Replace YOUR-PLAYLIST-ID, then preview without writing music:
suno-dl --url "https://suno.com/playlist/YOUR-PLAYLIST-ID" --dry-run
# Remove --dry-run when you are ready to download.

# Or open the local web UI and API:
suno-webui
```

Open **[localhost:8760](http://localhost:8760)** for the web UI.
[CLI recipes and configuration](docs/usage.md) · [Troubleshooting](docs/troubleshooting.md)

## Preview limits

- New downloads are M4A audio. Existing files are not renamed or retagged during sync.
  Older-file corrections need the [offline migration](docs/producer-migration.md)
  with all library writers stopped.
- Profile discovery can be incomplete. Use explicit playlist URLs when coverage matters.
  The optional [browser fallback](docs/usage.md#install) needs Chromium.
- Automatic repair, force replacement, rotation and retained video remain guarded.
  Windows/macOS archive writes are outside this release.

## Compatibility with existing installations

The package is `trackie-assistant`; commands `suno-dl`, `suno-webui`
and `suno-recommend` retain their names. Stop the old app and uninstall
`suno-download-cli` before installing the new package in the same environment.
Adding the HA repository creates a separate add-on installation; do not run two
writers against the same library. Upgrading does not migrate your music.

## More information

[How sync works](docs/verified-linux-sync.md) · [Artwork](docs/cover-art.md) ·
[Architecture](docs/architecture.md) · [Contributing](CONTRIBUTING.md) ·
[Security](SECURITY.md) · [Changelog](CHANGELOG.md)

Archive only music you own or have permission to download. Public sources only;
no account sign-in, cookies or private media decryption.

[MIT license](LICENSE).
